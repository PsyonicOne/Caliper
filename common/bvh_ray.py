import bmesh
import bpy
from mathutils.bvhtree import BVHTree

# Version marker — printed once when the module is loaded. If you don't
# see this when starting the measure tool, the .pyc cache is stale.
_BVH_RAY_VERSION = "v10-skip-pos-hash-big-2026"

print(f"[bvh_ray] loaded {_BVH_RAY_VERSION}")


# Threshold above which we skip the per-vertex position hash. The pure-
# Python `round()` call inside the hash costs ~9μs per vertex, so a 100k-
# vertex mesh takes ~900ms and a 260k-vertex mesh takes ~2.3 seconds on
# every single hover (including HITs). For meshes above this size the
# hash is replaced with a cheap constant — vertex moves with unchanged
# topology will no longer auto-invalidate the cache. The user can force
# a rebuild manually if needed (e.g. by re-invoking the operator, which
# resets `self.next_object` semantics, or via a future "force rebuild"
# shortcut).
_POSITION_HASH_VERT_LIMIT = 20000


def compute_fingerprint(obj):
    """
    Compute a lightweight fingerprint of an object that captures every
    aspect which would invalidate a cached BVH/bmesh:

      - ``mesh.name``            — catches mesh-data swap on the object
      - ``len(vertices/edges/faces)`` — catches topology changes
      - hash of all vertex positions — catches vertex moves with the same
        topology (skipped for meshes with > _POSITION_HASH_VERT_LIMIT
        verts because the pure-Python hash takes seconds on big meshes)
      - tuple of modifier types   — catches modifier-stack changes
        (modifiers affect the evaluated mesh)
      - world matrix              — catches object transforms, since the
        bmesh is cached in world space

    Cost: O(1) for everything except the optional position hash.
    Small meshes (< 20k verts): ~10-20ms total (foreach_get is C-speed,
    the round() + hash() chain runs in Python). Large meshes: ~1ms
    (position hash skipped, only O(1) parts run).

    :param obj: A Blender mesh object.
    :return:    A hashable tuple fingerprint, or ``None`` if the object
                can't be fingerprinted (e.g. not a mesh).
    """
    if obj is None or obj.type != 'MESH' or obj.data is None:
        return None

    mesh = obj.data

    # Topology counts — O(1)
    v_count = len(mesh.vertices)
    e_count = len(mesh.edges)
    # NOTE: Blender 5.0 removed Mesh.faces (it was deprecated in 2.63
    # in favour of Mesh.polygons, finally removed in 5.0). Use
    # polygons on the Mesh object, never faces.
    f_count = len(mesh.polygons)

    # Vertex position hash — O(n) when small enough to be cheap, or
    # constant-time sentinel for big meshes. Each `round()` call costs
    # ~9μs in pure Python, so 259k verts = ~2.3s on every hover.
    # Rounded to 4 decimal places so floating-point noise (e.g. from a
    # depsgraph refresh) doesn't false-trigger a rebuild.
    if v_count > _POSITION_HASH_VERT_LIMIT:
        pos_hash = 0  # Sentinel — large meshes skip position hash
    else:
        coords = [0.0] * (v_count * 3)
        mesh.vertices.foreach_get("co", coords)
        pos_hash = hash(tuple(round(c, 4) for c in coords))

    # Modifier stack — affects the evaluated mesh that the BVH is built from
    mod_types = tuple(m.type for m in obj.modifiers)

    # World matrix — bmesh is cached in world space, so a transform
    # change invalidates it. Round to 6dp to ignore sub-pixel jitter.
    mw = obj.matrix_world
    transform = (
        round(mw[0][0], 6), round(mw[0][1], 6), round(mw[0][2], 6), round(mw[0][3], 6),
        round(mw[1][0], 6), round(mw[1][1], 6), round(mw[1][2], 6), round(mw[1][3], 6),
        round(mw[2][0], 6), round(mw[2][1], 6), round(mw[2][2], 6), round(mw[2][3], 6),
        round(mw[3][0], 6), round(mw[3][1], 6), round(mw[3][2], 6), round(mw[3][3], 6),
    )

    return (mesh.name, v_count, e_count, f_count, pos_hash, mod_types, transform)


def preprocess_object_for_raycast(context, obj):
    """
    Pre-process an object for fast ray-cast hit detection.

    Pipeline:
    1. Create bmesh from evaluated object (includes modifiers)
    2. Transform to world space
    3. Build the BVH tree

    That's it. ``BVHTree.FromBMesh`` handles quads and ngons natively —
    it internally fans each face into triangles for the BVH, but the
    ``face_index`` it returns refers to the original face in the bmesh
    you passed in, not the internal triangle. So we get correct
    face-indexing for free, with no need to:
      - copy the bmesh
      - triangulate it
      - build a tri-to-orig index map

    The single bmesh is used for:
      - BVH ray-casting (data[0])
      - face/edge/vertex lookups in edge mode, vertex mode, face mode
      - circle detection (edge-loop walks see the real mesh edges, not
        spurious diagonals)
      - coplanar face grouping (sees the real quad/ngon adjacency)

    :param context: Blender context
    :param obj:     The object to pre-process
    :return:        (BVHTree, BMesh) tuple in world space, or None on
                    failure.
    """
    if obj is None or obj.type != 'MESH':
        return None

    # Step 1: Create bmesh from evaluated object (local space, includes
    # modifiers).
    depsgraph = context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(depsgraph)
    bm = bmesh.new()
    bm.from_mesh(eval_obj.data)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    # Step 2: Transform to world space so BVH ray-casts work in the
    # same coordinate system as the scene.ray_cast.
    bm.transform(obj.matrix_world)

    # Step 3: Build BVH tree. FromBMesh handles quads/ngons natively.
    try:
        bvh = BVHTree.FromBMesh(bm)
    except Exception as e:
        print(f"[bvh_ray] BVH build failed for {obj.name!r}: "
              f"{type(e).__name__}: {e}")
        bm.free()
        return None

    return bvh, bm


class BVHRay:
    def __init__(self, context, objects, mode):
        self.mode = mode
        self.bvh_data = self._build_bvh_trees(context, objects)

    @classmethod
    def from_preprocessed(cls, obj, bvh, bm):
        """
        Create a BVHRay wrapper from already-built BVH and bmesh data.

        Used when the BVH has been pre-processed by
        preprocess_object_for_raycast() and is being served from a
        persistent per-object cache. Avoids re-running the (expensive)
        pre-processing pipeline on every hover.

        Stores:
          - bvh: the BVHTree (C-level, built from the bmesh directly)
          - bm:  the bmesh (single source of truth — used for BVH hits
                 AND for face/edge/vertex lookups, coplanar grouping,
                 and circle detection)

        Single bmesh, no triangulated copy, no index map. The BVH's
        ``face_index`` already refers to this bmesh's faces.
        """
        instance = cls.__new__(cls)
        instance.bvh_data = {obj: [bvh, bm]}
        return instance

    def _build_bvh_trees(self, context, objects):
        """
        Build a BVH tree for each object

        :param objects: List of objects with mesh data
        :return: Dictionary containing:
        :        key = object
        :        value = list[]
        :                list[0] = bvh tree of object
        :                list[1] = bmesh of object
        """
        bvh_data_local = {}

        # check mode
        if self.mode == 'MULTIPLE':
            for obj in objects:
                if obj.type != 'MESH':
                    continue
                if obj.data.is_editmode:
                    # object in edit mode
                    # create a bmesh of non-evaluated object
                    bm_orig = bmesh.from_edit_mesh(obj.data)
                else:
                    # object in object mode
                    # create a bmesh of evaluated object
                    depsgraph = context.evaluated_depsgraph_get()
                    eval_obj = obj.evaluated_get(depsgraph)
                    # print("eval_obj", eval_obj)
                    bm_orig = bmesh.new()
                    bm_orig.from_mesh(eval_obj.data)
                # Ensure the bmesh is up to date
                bm_orig.verts.ensure_lookup_table()
                bm_orig.edges.ensure_lookup_table()
                bm_orig.faces.ensure_lookup_table()
                # bm.transform(obj.matrix_world)

                # Create a new BMesh
                bm_ray_scan = bmesh.new()

                # Dictionary to map old vertices to new vertices
                vert_map = {}
                edge_map = {}
                face_map = {}

                # Copy only visible faces and their associated vertices/edges
                for face in bm_orig.faces:
                    if not face.hide:  # Exclude hidden faces
                        # Copy vertices
                        face_verts = []
                        for vert in face.verts:
                            if vert not in vert_map:
                                new_vert = bm_ray_scan.verts.new(vert.co)
                                vert_map[vert] = new_vert
                            face_verts.append(vert_map[vert])

                        for edge in face.edges:
                            if edge not in edge_map:
                                new_edge_verts = (vert_map[edge.verts[0]], vert_map[edge.verts[1]])
                                new_edge = bm_ray_scan.edges.new(new_edge_verts)
                                edge_map[edge] = new_edge

                        # Create the face in the new BMesh
                        new_face = bm_ray_scan.faces.new(face_verts)
                        if new_face not in face_map:
                            face_map[face] = new_face

                # Ensure the bmesh is up to date
                bm_ray_scan.verts.ensure_lookup_table()
                bm_ray_scan.edges.ensure_lookup_table()
                bm_ray_scan.faces.ensure_lookup_table()
                print("num edges:", len(bm_ray_scan.edges))

                # Preserve edge and face indices
                for old_edge, new_edge in edge_map.items():
                    new_edge.index = old_edge.index
                # Ensure the BMesh's indices reflect the updated mapping
                bm_ray_scan.edges.sort()

                for old_face, new_face in face_map.items():
                    new_face.index = old_face.index
                # Ensure the BMesh's indices reflect the updated mapping
                bm_ray_scan.faces.sort()

                # Ensure the bmesh is up to date
                bm_ray_scan.verts.ensure_lookup_table()
                bm_ray_scan.edges.ensure_lookup_table()
                bm_ray_scan.faces.ensure_lookup_table()

                bm_ray_scan.transform(obj.matrix_world)

                # Create a BVH tree from the bmesh
                bvh = BVHTree.FromBMesh(bm_ray_scan)

                # Store the BVH tree and the bmesh for the object
                # obj = objects name - an entry in the dict is created for each object in objects
                bvh_data_local[obj] = [bvh, bm_orig, bm_ray_scan]

        if self.mode == 'SINGLE':
            # print("creating new MEASURE bvh")
            if objects.type != 'MESH':
                self.report({"INFO"}, "Only 1 object may be selected")
                return None
            bm_orig = bmesh.new()
            bm_ray_scan = bmesh.new()
            # if objects.mode == 'EDIT':
            if objects.data.is_editmode:
                objects.update_from_editmode()
                bm_orig.from_mesh(objects.data)
            else:
                depsgraph = context.evaluated_depsgraph_get()
                # bm_orig.from_mesh(context.view_layer.depsgraph.objects[objects.name].data)
                eval_obj = objects.evaluated_get(depsgraph)
                # print("eval_obj", eval_obj)
                bm_orig.from_mesh(eval_obj.data)
            bm_orig.transform(objects.matrix_world)

            # Ensure the bmesh is up to date
            bm_orig.verts.ensure_lookup_table()
            bm_orig.edges.ensure_lookup_table()
            bm_orig.faces.ensure_lookup_table()
            bm_orig.verts.index_update()
            bm_orig.edges.index_update()
            bm_orig.faces.index_update()

            # Create a BVH tree from the bmesh
            bvh = BVHTree.FromBMesh(bm_orig)

            # Store the BVH tree and the bmesh for the object
            bvh_data_local[objects] = [bvh, bm_orig, bm_ray_scan]

        return bvh_data_local

    def ray_cast_bvh(self, ray_origin, ray_direction):
        """
        Perform a ray cast against multiple BVH trees.

        The BVH is built directly from the (untriangulated) bmesh, so
        the ``face_index`` returned by ``BVHTree.ray_cast`` already
        refers to the correct face in the bmesh — no index translation
        needed.

        :param ray_origin: Origin of the ray (Vector).
        :param ray_direction: Direction of the ray (Vector).
        :return: Dictionary with details of the hit or None if no hit.
        """
        closest_hit = None

        for obj, data in self.bvh_data.items():
            # data = [bvh, bmesh]
            bvh = data[0]
            bm = data[1]

            # Perform ray cast on this BVH tree
            hit_location, hit_normal, face_index, hit_distance = (
                bvh.ray_cast(ray_origin, ray_direction)
            )

            if hit_location:
                closest_hit = {
                    "object": obj,
                    "bmesh_orig": bm,
                    "face_index": face_index,
                    "location": hit_location,
                    "normal": hit_normal,
                    "distance": hit_distance,
                }

        return closest_hit
