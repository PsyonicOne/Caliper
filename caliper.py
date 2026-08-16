import bpy
import bmesh
import math
import time
from bpy_extras import view3d_utils
from bpy.types import Operator
from mathutils import Vector
from mathutils.geometry import intersect_point_line
from .common import drawer
from .common import bvh_ray
from .common import handlers


# Module-level BVH cache. Keyed by object name (str) ->
# (BVHRay wrapper, fingerprint) tuple. Cleared on tool exit
# (RMB/ESC) so memory is released between measure-tool invocations.s
_BVH_OBJECT_CACHE = {}


class CALIPER_OT_measure(Operator):
    bl_idname = "caliper.measure"
    bl_label = "Caliper"
    bl_description = "Measure Distance or Angle (auto-detects circles)"
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    # bl_options = {"BLOCKING", "REGISTER", "UNDO"}  # BLOCKING prevents viewport rotation
    bl_options = {"REGISTER", "UNDO"}

    measure_modes = {
        'distance': 'd',
        'angle': 'a',
    }
    selection_modes = {
        1: 'face',
        2: 'edge',
        3: 'vertex',
    }

    @classmethod
    def poll(cls, context):
        return context.area.type == "VIEW_3D"

    def invoke(self, context, event):
        props = context.scene.caliper_props
        self.mode = props.measure_mode
        self.selection_mode = props.selection_mode
        self.measurement_phase = 'idle'  # 'idle', 'first_selected', 'complete'

        # Save and replace header for live controls during modal operation
        self.old_header = bpy.types.VIEW3D_HT_header.draw
        bpy.types.VIEW3D_HT_header.draw = draw_measure_header
        self.bvh_data = None
        self.objects_hidden = []
        self.circle_drawer_coords = []
        self.tag_drawer_coords = []
        self.tol_drawer_coords = []
        self.highlight_coords = []
        self.measure_line_coords = []
        self.first_element = None  # {type, point, object, bm_data, ...}
        self.current_hit_data = None
        self.circle_centre = None
        self.circle_dia = ""
        self.next_object = False
        self._selected_coords = []  # Accumulated coords for persistent highlights
        self._coplanar_cache = {}  # Cache coplanar face group results (per face_index)
        # _BVH_OBJECT_CACHE is module-level (see top of file). It is
        # cleared on tool exit (see RMB/ESC handler below) so memory is
        # released between measure-tool invocations.
        self._last_face_key = None  # Track last hovered face to skip rebuild on same-face wiggles
        self._second_element = None  # Second endpoint data in angle mode (for re-rendering the final angle)
        self._last_show_xyz = props.show_xyz  # Track XYZ toggle state
        self.rotating_view = False  # Flag to track when viewport is being manipulated
        self._prev_view_matrix = None  # Cached view matrix for rotation detection
        self._matrix_stable_count = 0   # Consecutive frames with same view matrix

        # Colours
        self.colour_loop = (1.0, 1.0, 0.0, 1.0)
        self.colour_tag = (0.0, 1.0, 1.0, 1.0)
        self.colour_tag_text = (1.0, 1.0, 0.8, 1.0)
        self.colour_highlight = (0.0, 0.8, 1.0, 1.0)
        # Face fill: same cyan as the outline, 20% alpha so the underlying
        # geometry stays visible underneath the fill. Tinted the same hue so
        # the outline and fill read as one highlight.
        self.colour_highlight_face = (0.0, 0.8, 1.0, 0.2)
        self.colour_measure_line = (1.0, 1.0, 0.0, 1.0)  # Yellow main measure line
        self.colour_measure_text = (0.96, 0.6, 0.76, 1.0)
        self.colour_measure_point = (1.0, 0.5, 0.0, 1.0)
        self.colour_measure_point_face = (1.0, 0.5, 0.0, 0.2)
        self.colour_axis_x = (1.0, 0.0, 0.0, 1.0)  # Red for X axis component
        self.colour_axis_y = (0.0, 1.0, 0.0, 1.0)  # Green for Y axis component
        self.colour_axis_z = (0.0, 0.5, 1.0, 1.0)  # #0080FF lighter blue for Z axis

        context.area.header_text_set(
            "Distance Mode (auto-detects circles in face/edge) | LMB: Select (SHIFT: no circle) | RMB: Cancel | D: Distance | A: Angle | 1/2/3: Selection")

        self.props = context.scene.caliper_props
        if self.props.path_tol is not None:
            self.path_tolerance = self.props.path_tol
        if self.props.circle_tol is not None:
            self.circle_tolerance = self.props.circle_tol

        handlers.DrawHandlerManager.remove_all_handlers()

        # Text drawer for mode status / tolerance
        self.tag_drawer = drawer.ScreenDrawer(
            "tag_drawer", self.tag_drawer_coords,
            line_colour=self.colour_tag, line_width=3.0,
            text="", text_colour=self.colour_tag_text,
            text_size=30, text_pos_x=1, text_pos_y=1)

        # Tolerance display drawers (always visible for tuning circle detection)
        self.path_tol_drawer = drawer.ScreenDrawer(
            "path_tol_drawer", self.tol_drawer_coords,
            line_colour=self.colour_tag, line_width=25.0,
            text="", text_colour=self.colour_tag,
            text_size=20, text_pos_x=1, text_pos_y=1)
        self.path_tol_drawer.update_text("Path Tol: " + str(round(self.path_tolerance, 2)))

        self.circle_tol_drawer = drawer.ScreenDrawer(
            "circle_tol_drawer", self.tol_drawer_coords,
            line_colour=self.colour_tag, line_width=25.0,
            text="", text_colour=self.colour_tag,
            text_size=20, text_pos_x=1, text_pos_y=1)
        self.circle_tol_drawer.update_text("Circle Tol: " + str(round(self.circle_tolerance, 2)))

        # Highlight overlay (face fill, edge highlight, vertex marker)
        self.highlight_drawer = drawer.ScreenDrawer(
            "highlight_drawer", self.highlight_coords,
            line_colour=self.colour_highlight, line_width=6.0,
            text="", text_colour=self.colour_highlight,
            text_size=20, text_pos_x=1, text_pos_y=1)

        # Face-fill overlay for the coplanar group (semi-transparent triangles
        # drawn in TRIS mode so the underlying geometry is still visible).
        # Shares the same colour as the outline highlight, just with low alpha.
        self.face_fill_coords = []
        self.face_fill_drawer = drawer.ScreenDrawer(
            "face_fill_drawer", self.face_fill_coords,
            line_colour=self.colour_highlight_face, line_width=1.0,
            text="", text_colour=self.colour_highlight_face,
            text_size=20, text_pos_x=1, text_pos_y=1,
            draw_type='TRIS')

        # Selected highlight drawer (persists after selection)
        self.selected_highlight_drawer = drawer.ScreenDrawer(
            "selected_highlight_drawer", [],
            line_colour=self.colour_measure_point, line_width=6.0,
            text="", text_colour=self.colour_measure_point,
            text_size=20, text_pos_x=1, text_pos_y=1)
        # Selected face fill (translucent orange — same role as the
        # cyan face_fill_drawer but for the post-click persistent
        # highlight). Accumulates triangulated coords across multiple
        # clicks so each selected face keeps its own translucency.
        self._selected_fill_coords = []
        self.selected_fill_drawer = drawer.ScreenDrawer(
            "selected_fill_drawer", self._selected_fill_coords,
            line_colour=self.colour_measure_point_face, line_width=1.0,
            text="", text_colour=self.colour_measure_point_face,
            text_size=20, text_pos_x=1, text_pos_y=1,
            draw_type='TRIS')

        # Measurement line drawer
        self.measure_line_drawer = drawer.ScreenDrawer(
            "measure_line_drawer", self.measure_line_coords,
            line_colour=self.colour_measure_line, line_width=4.0,
            text="", text_colour=self.colour_measure_text,
            text_size=22, text_pos_x=1, text_pos_y=1)

        # Measurement text (shown at midpoint of line)
        self.measure_text_drawer = drawer.ScreenDrawer(
            "measure_text_drawer", self.measure_line_coords,
            line_colour=self.colour_measure_text, line_width=3.0,
            text="", text_colour=self.colour_measure_text,
            text_size=24, text_pos_x=1, text_pos_y=20)

        # Axis component lines (X=red, Y=green, Z=blue)
        self.axis_x_coords = []
        self.axis_y_coords = []
        self.axis_z_coords = []
        self.axis_x_drawer = drawer.ScreenDrawer(
            "axis_x_drawer", self.axis_x_coords,
            line_colour=self.colour_axis_x, line_width=3.0,
            text="X: ", text_colour=self.colour_measure_text,
            text_size=16, text_pos_x=1, text_pos_y=1)
        self.axis_y_drawer = drawer.ScreenDrawer(
            "axis_y_drawer", self.axis_y_coords,
            line_colour=self.colour_axis_y, line_width=3.0,
            text="Y: ", text_colour=self.colour_measure_text,
            text_size=16, text_pos_x=1, text_pos_y=1)
        self.axis_z_drawer = drawer.ScreenDrawer(
            "axis_z_drawer", self.axis_z_coords,
            line_colour=self.colour_axis_z, line_width=3.0,
            text="Z: ", text_colour=self.colour_measure_text,
            text_size=16, text_pos_x=1, text_pos_y=1)

        # XYZ data widget — screen-space overlay anchored to the lower-left
        # corner of the viewport. Each row is (text, colour); updated by
        # _update_axis_lines whenever a measurement is active.
        self.xyz_overlay_drawer = drawer.ScreenDrawer(
            "xyz_overlay_drawer", [],
            line_colour=(0, 0, 0, 0), line_width=1.0,
            text="", text_colour=self.colour_measure_text,
            text_size=18, text_pos_x=1, text_pos_y=1)

        self.objects_visible = [obj.name for obj in context.scene.objects if obj.visible_get()]
        self.scene_units = context.scene.unit_settings.system
        context.window.cursor_set("PICK_AREA")
        context.window_manager.modal_handler_add(self)

        # Enable tolerance drawers for circle detection (always available)
        self.path_tol_drawer.enable()
        self.circle_tol_drawer.enable()

        # XYZ data widget is always shown while the tool is active —
        # "Show XYZ" only toggles the 3D axis lines. Show 0.0 placeholders
        # until a measurement is in progress / completed.
        self.xyz_overlay_drawer.enable()
        self._show_default_xyz_overlay()

        self._update_status_text(context)
        return {"RUNNING_MODAL"}

    # ──────────────── STATUS / UI HELPERS ────────────────

    def _update_status_text(self, context):
        mode_names = {'distance': 'Distance', 'angle': 'Angle'}
        sel_names = {'face': 'Face', 'edge': 'Edge', 'vertex': 'Vertex'}
        text = f"[{mode_names[self.mode]}] Sel: {sel_names[self.selection_mode]} | D: Distance | A: Angle | 1/2/3: Selection | LMB: Select (SHIFT: no circle) | RMB: Cancel"
        context.area.header_text_set(text)

    def _format_distance(self, value):
        """Format a distance value according to scene units."""
        if self.scene_units == 'IMPERIAL':
            return f"{value / 25.4:.3f}\""
        else:
            return f"{value:.2f}mm"

    def _format_angle(self, degrees):
        """Format an angle in degrees."""
        return f"{degrees:.1f}°"

    def _compute_snap_point(self, hit_data):
        """
        Given hit data (face_index, bmesh, location, object),
        return the snap point based on selection_mode.
        """
        if hit_data is None:
            return None

        bm_orig = hit_data.get('bmesh_orig')
        face_index = hit_data.get('face_index')
        hit_location = hit_data.get('location')

        if bm_orig is None or face_index is None:
            return hit_location

        if face_index >= len(bm_orig.faces):
            return hit_location

        face = bm_orig.faces[face_index]

        if self.selection_mode == 'vertex':
            # Find closest vertex on the face
            closest_vert = None
            closest_dist = float('inf')
            for vert in face.verts:
                dist = (vert.co - hit_location).length
                if dist < closest_dist:
                    closest_dist = dist
                    closest_vert = vert
            return closest_vert.co if closest_vert else hit_location

        elif self.selection_mode == 'edge':
            # Find closest edge on the face, snap to its center
            closest_edge = None
            closest_dist = float('inf')
            for edge in face.edges:
                e1 = edge.verts[0].co
                e2 = edge.verts[1].co
                closest_point, _ = intersect_point_line(hit_location, e1, e2)
                if (closest_point - e1).dot(e2 - e1) < 0:
                    closest_point = e1
                elif (closest_point - e2).dot(e1 - e2) < 0:
                    closest_point = e2
                dist = (hit_location - closest_point).length
                if dist < closest_dist:
                    closest_dist = dist
                    closest_edge = edge

            if closest_edge:
                return (closest_edge.verts[0].co + closest_edge.verts[1].co) / 2
            return hit_location

        elif self.selection_mode == 'face':
            # Snap to the coplanar face-group centroid (average of all
            # unique vertex positions across every connected coplanar
            # face), not just the active face. This means a click on any
            # face in a co-planar surface selects the group's geometric
            # centre — matching what the cyan face-group highlight
            # visually shows the user.
            #
            # NOTE on coordinate space: bvh_ray.py calls
            # bm_orig.transform(obj.matrix_world) when building the
            # bmesh_orig, so its vertex coordinates are already in world
            # space — no additional transform is needed here. (Earlier I
            # added a matrix_world transform that double-applied and
            # threw the snap point off to one side of the object.)
            # NOTE: pass cache_key=None here on purpose. _compute_snap_point
            # is called on every MOUSEMOVE; caching with triangulate=False
            # would store tri_coords=[] in the shared cache, which the
            # cyan face-fill highlight relies on (it asks for
            # triangulate=True and reuses whatever's cached). Sharing the
            # cache here would silently break the cyan face-fill on the
            # first hover after a click. The BFS cost (~5-15ms) on every
            # hover is acceptable; if it becomes a hotspot we can
            # refactor to a separate cache key.
            face_set, _boundary, _edge_count, _tri = self._get_coplanar_face_group(
                bm_orig, face_index, angle_tolerance_deg=0.5,
                cache_key=None, triangulate=False)
            if face_set:
                # Dedup vertices by rounded world-space coordinate (not
                # id(v)) because BMElements accessed through different
                # face iteration paths can yield distinct Python wrappers
                # for the same logical vertex. Quantising to 1e-4 keeps
                # floating-point drift from creating spurious duplicates.
                seen_verts = set()
                centroid = Vector((0, 0, 0))
                count = 0
                for f in face_set:
                    try:
                        verts = list(f.verts)
                    except ReferenceError:
                        continue
                    for v in verts:
                        try:
                            co = v.co
                        except ReferenceError:
                            continue
                        key = (round(co.x, 4), round(co.y, 4), round(co.z, 4))
                        if key in seen_verts:
                            continue
                        seen_verts.add(key)
                        centroid += co
                        count += 1
                if count > 0:
                    return centroid / count
            # Fallback: single-face centroid if the coplanar lookup fails.
            centroid = Vector((0, 0, 0))
            for vert in face.verts:
                centroid += vert.co
            return centroid / len(face.verts)

        return hit_location

    def _get_edge_data(self, hit_data):
        """Extract edge information from hit data. Returns (edge, edge_center, edge_direction)."""
        bm_orig = hit_data.get('bmesh_orig')
        face_index = hit_data.get('face_index')
        hit_location = hit_data.get('location')

        if bm_orig is None or face_index is None or face_index >= len(bm_orig.faces):
            return None

        face = bm_orig.faces[face_index]

        closest_edge = None
        closest_dist = float('inf')
        for edge in face.edges:
            e1 = edge.verts[0].co
            e2 = edge.verts[1].co
            closest_point, _ = intersect_point_line(hit_location, e1, e2)
            if (closest_point - e1).dot(e2 - e1) < 0:
                closest_point = e1
            elif (closest_point - e2).dot(e1 - e2) < 0:
                closest_point = e2
            dist = (hit_location - closest_point).length
            if dist < closest_dist:
                closest_dist = dist
                closest_edge = edge

        if closest_edge:
            center = (closest_edge.verts[0].co + closest_edge.verts[1].co) / 2
            direction = (closest_edge.verts[1].co - closest_edge.verts[0].co).normalized()
            return (closest_edge, center, direction)

        return None

    def _get_face_data(self, hit_data):
        """Extract face information from hit data. Returns (face, face_center, face_normal)."""
        bm_orig = hit_data.get('bmesh_orig')
        face_index = hit_data.get('face_index')

        if bm_orig is None or face_index is None or face_index >= len(bm_orig.faces):
            return None

        face = bm_orig.faces[face_index]
        centroid = Vector((0, 0, 0))
        for vert in face.verts:
            centroid += vert.co
        centroid /= len(face.verts)

        return (face, centroid, face.normal.copy())

    def _collect_edge_loop(self, start_edge):
        """Walk a connected chain of edges starting from start_edge,
        following edges that share a vertex. Returns a list of BMEdge
        objects representing the full loop.

        Handles both closed loops (returns when traversal returns to
        start_edge) and open paths (returns when no further edge
        connects at the current endpoint). T-junctions and branch
        points terminate the walk — each visited vertex contributes at
        most two edges to the loop (one in, one out).

        Used by the edge-mode selected highlight to mirror Blender's
        native "Select Edge Loop" behaviour.
        """
        if start_edge is None:
            return []

        def next_edge_from(edge, via_vert):
            """Find the edge connected to via_vert that isn't edge."""
            for le in via_vert.link_edges:
                if le is not edge:
                    return le
            return None

        loop = [start_edge]
        # Walk forward through verts[0] -> verts[1]
        edge = start_edge
        vert = edge.verts[0]
        for _ in range(10000):  # generous safety cap for very long loops
            nxt = next_edge_from(edge, vert)
            if nxt is None or nxt is start_edge:
                break
            # If nxt is already in the loop (we doubled back), stop.
            if nxt in loop:
                break
            loop.append(nxt)
            # Step to the other vertex of nxt.
            other = nxt.other_vert(vert)
            vert = other
            edge = nxt
        # Walk backward through verts[1] -> verts[0]
        edge = start_edge
        vert = edge.verts[1]
        for _ in range(10000):
            nxt = next_edge_from(edge, vert)
            if nxt is None or nxt is start_edge:
                break
            if nxt in loop:
                break
            loop.insert(0, nxt)
            other = nxt.other_vert(vert)
            vert = other
            edge = nxt
        return loop

    # ──────────────── COPLANAR FACE GROUPING ────────────────

    def _triangulate_via_quads_convert_sync(self, face_set, bm):
        """
        Synchronous triangulation for the coplanar face group using
        bpy.ops.mesh.quads_convert_to_tris(quad_method='BEAUTY',
        ngon_method='BEAUTY') — the only algorithm that correctly
        handles ngons with holes on the cylindrical end-cap seen in
        the bug report (bmesh.ops.triangulate with any ngon_method
        gives visibly wrong hole edge loops).

        Runs directly from the modal handler. For each new face the
        cost is ~5-15ms (operator round trip + bmesh rebuild) which
        is fine on small meshes but expensive on dense meshes — that
        cost is amortised by self._coplanar_cache so repeated hovers
        on the same face are 0ms.

        Returns flat list of Vector tuples ready for the TRIS drawer,
        or [] on failure.
        """
        if not face_set:
            return []

        # Build a fresh bmesh from the coplanar face group. Vertices
        # in  are already in world space (BVH preprocess transforms
        # the whole bmesh by matrix_world), so reading v.co directly
        # gives the right coords.
        tmp_bm = bmesh.new()
        vert_map = {}
        for face in face_set:
            try:
                src_verts = list(face.verts)
            except ReferenceError:
                continue
            new_verts = []
            for v in src_verts:
                try:
                    co = v.co.copy()
                except ReferenceError:
                    new_verts = []
                    break
                if v not in vert_map:
                    vert_map[v] = tmp_bm.verts.new(co)
                new_verts.append(vert_map[v])
            if len(new_verts) >= 3:
                try:
                    new_face = tmp_bm.faces.new(new_verts)
                except ValueError:
                    new_face = None
                if new_face is not None:
                    # Re-add interior hole-boundary edges from the
                    # source face (those that don't lie on the outer
                    # loop). Adding such an edge on a fresh face
                    # automatically splits the face along its interior
                    # boundary, so the resulting Mesh has the hole as
                    # a polygon island — which is what the operator
                    # needs to triangulate correctly.
                    try:
                        outer_set = set(face.verts)
                        inner_pairs = [
                            (e.verts[0], e.verts[1])
                            for e in face.edges
                            if not set(e.verts).issubset(outer_set)
                        ]
                    except ReferenceError:
                        inner_pairs = []
                    for v0, v1 in inner_pairs:
                        try:
                            a = vert_map.get(v0) or tmp_bm.verts.new(v0.co.copy())
                            b = vert_map.get(v1) or tmp_bm.verts.new(v1.co.copy())
                            try:
                                tmp_bm.edges.new([a, b])
                            except ValueError:
                                pass
                        except ReferenceError:
                            continue

        if not tmp_bm.faces:
            tmp_bm.free()
            return []

        # Pre-select every face/edge/vert in the bmesh so the select
        # state rides through to_mesh(). This avoids having to call
        # bpy.ops.mesh.select_all(action='SELECT') inside the
        # temp_override from the modal handler — its .poll() refuses
        # to run there. quads_convert_to_tris operates on the selection,
        # so we need this for the operator to act on anything.
        for f in tmp_bm.faces:
            f.select = True
        for e in tmp_bm.edges:
            e.select = True
        for v in tmp_bm.verts:
            v.select = True

        # Realise and wrap in a temp Object.
        tmp_mesh = bpy.data.meshes.new('_measure_coplanar_tmp_mesh')
        tmp_bm.to_mesh(tmp_mesh)
        tmp_bm.free()

        tmp_obj = bpy.data.objects.new('_measure_coplanar_tmp', tmp_mesh)
        tmp_obj.hide_render = True
        try:
            bpy.context.view_layer.active_layer_collection.collection.objects.link(tmp_obj)
        except RuntimeError:
            pass

        # Save the user's active object, selection set, and current
        # mode so we can restore them after the triangulation dance
        # below. The temp object we create and link temporarily takes
        # over the active slot, AND `bpy.ops.object.mode_set()` is a
        # global mode operator — when the user is in edit mode on
        # their own object, the implicit mode switch on triangulation
        # pulls them out of edit mode. We have to put them back in.
        saved_active = bpy.context.view_layer.objects.active
        saved_selection = [o for o in bpy.context.selected_objects]
        # Capture the user's mode BEFORE any temp-object manipulation.
        # Mode is a single global value on the current screen, so we
        # save it once and restore it once at the end.
        # tool_set_by_id may not be available in all versions, so
        # fall back to a manual mode_set.
        saved_mode = None
        if saved_active is not None:
            try:
                if saved_active.mode == 'EDIT':
                    saved_mode = 'EDIT'
                elif saved_active.mode == 'SCULPT':
                    saved_mode = 'SCULPT'
                elif saved_active.mode == 'VERTEX_PAINT':
                    saved_mode = 'VERTEX_PAINT'
                elif saved_active.mode == 'WEIGHT_PAINT':
                    saved_mode = 'WEIGHT_PAINT'
                elif saved_active.mode == 'POSE':
                    saved_mode = 'POSE'
            except (ReferenceError, AttributeError):
                pass

        tri_coords = []
        try:
            tmp_obj.select_set(True)
            bpy.context.view_layer.objects.active = tmp_obj
            bpy.ops.object.mode_set(mode='EDIT')

            bpy.ops.mesh.quads_convert_to_tris(
                quad_method='BEAUTY',
                ngon_method='BEAUTY',
            )
            tmp_obj.update_from_editmode()

            for poly in tmp_mesh.polygons:
                if len(poly.vertices) != 3:
                    continue
                for vi in poly.vertices:
                    v = tmp_mesh.vertices[vi].co
                    tri_coords.append(Vector((v.x, v.y, v.z)))

            bpy.ops.object.mode_set(mode='OBJECT')
        except RuntimeError:
            pass
        finally:
            # Restore the user's selection state BEFORE removing the
            # temp object. If we removed first, view_layer.objects.active
            # would already point at the deleted temp obj's last slot,
            # and Blender would auto-resolve it to something else.
            try:
                # Deselect everything we touched, then re-select the
                # user's original selection and re-mark the active.
                for o in list(bpy.context.selected_objects):
                    try:
                        o.select_set(False)
                    except ReferenceError:
                        pass
                for o in saved_selection:
                    try:
                        if o.name in bpy.data.objects:
                            o.select_set(True)
                    except ReferenceError:
                        pass
                if saved_active is not None and saved_active.name in bpy.data.objects:
                    bpy.context.view_layer.objects.active = saved_active
                # If the user was in edit mode (or another non-Object
                # mode) when we started, restore that mode AFTER the
                # active object is set back. This was the regression —
                # `bpy.ops.object.mode_set('EDIT')` on the temp object
                # implicitly switched the user's object out of edit
                # mode, and `mode_set('OBJECT')` left them in Object
                # Mode. Re-applying the saved mode puts them back.
                if saved_mode is not None and saved_active is not None:
                    if saved_active.name in bpy.data.objects:
                        try:
                            bpy.ops.object.mode_set(mode=saved_mode)
                        except (ReferenceError, RuntimeError):
                            pass
            except (ReferenceError, RuntimeError):
                pass
            try:
                bpy.data.objects.remove(tmp_obj, do_unlink=True)
            except (ReferenceError, RuntimeError):
                pass
            try:
                bpy.data.meshes.remove(tmp_mesh)
            except (ReferenceError, RuntimeError):
                pass

        return tri_coords

    def _get_coplanar_face_group(self, bm, start_face_index, angle_tolerance_deg=1.0, cache_key=None, triangulate=False, context=None):
        """
        Starting from the face at start_face_index, flood-fill through connected faces
        that are coplanar (within angle_tolerance_deg). Returns:
          - face_set: set of Face objects in the coplanar group (LIVE BMesh refs, use immediately)
          - boundary_edges: list of (v1, v2) coordinate tuples
          - edge_face_count: dict mapping edges to the number of group faces sharing them
          - tri_coords: flat list of Vector tuples (world space) ready for
                        the TRIS face-fill drawer. Empty when triangulate=False,
                        the triangulation hasn't completed yet, or no
                        faces were found.

        When `triangulate=True` and a cache_key is supplied, the result is
        computed synchronously via _triangulate_via_quads_convert_sync,
        which uses bpy.ops.mesh.quads_convert_to_tris(quad_method=
        'BEAUTY', ngon_method='BEAUTY'). The 4th tuple element holds
        the resulting triangle coordinates (or [] on failure). The
        whole entry is cached so repeated hovers on the same face are
        free.
        """
        if cache_key is not None and cache_key in self._coplanar_cache:
            return self._coplanar_cache[cache_key]

        if bm is None or start_face_index >= len(bm.faces):
            result = (set(), [], {}, [])
            if cache_key is not None:
                self._coplanar_cache[cache_key] = result
            return result

        start_face = bm.faces[start_face_index]
        ref_normal = start_face.normal.copy()
        angle_threshold_rad = math.radians(angle_tolerance_deg)

        # Guard: skip if reference face has zero-length normal (degenerate face)
        if ref_normal.length_squared < 0.0001:
            result = ({start_face}, list(start_face.edges), {}, [])
            if cache_key is not None:
                self._coplanar_cache[cache_key] = result
            return result

        # BFS flood-fill through EDGE-CONNECTED coplanar faces only.
        face_set = {start_face}
        frontier = [start_face]
        visited = {start_face}

        while frontier:
            current_face = frontier.pop()
            for edge in current_face.edges:
                for linked_face in edge.link_faces:
                    if linked_face in visited:
                        continue
                    visited.add(linked_face)
                    # Guard: skip linked faces with zero-length normals
                    if linked_face.normal.length_squared < 0.0001:
                        continue
                    # Check if normal is within tolerance of reference normal
                    if ref_normal.angle(linked_face.normal) <= angle_threshold_rad:
                        face_set.add(linked_face)
                        frontier.append(linked_face)

        # Count how many group faces share each edge (used for boundary detection)
        edge_face_count = {}
        for face in face_set:
            for edge in face.edges:
                edge_face_count[edge] = edge_face_count.get(edge, 0) + 1

        boundary_edges = [edge for edge, count in edge_face_count.items() if count == 1]

        # Convert to safe immutable coordinate tuples to avoid ReferenceError
        safe_boundary = []
        for edge in boundary_edges:
            try:
                safe_boundary.append((
                    edge.verts[0].co.copy(),
                    edge.verts[1].co.copy(),
                ))
            except ReferenceError:
                pass

        # Triangulate the coplanar faces synchronously via
        # bpy.ops.mesh.quads_convert_to_tris(quad_method='BEAUTY',
        # ngon_method='BEAUTY'). This is the only algorithm that
        # correctly handles ngons with holes on the cylindrical end-cap
        # seen in the bug report — bmesh.ops.triangulate with any
        # ngon_method produces visibly wrong hole edge loops.
        tri_coords = []
        if triangulate and cache_key is not None:
            try:
                tri_coords = self._triangulate_via_quads_convert_sync(
                    face_set, bm)
            except Exception:
                tri_coords = []

        result = (face_set, safe_boundary, edge_face_count, tri_coords)
        if cache_key is not None:
            self._coplanar_cache[cache_key] = result
        return result

    def _build_vertex_crosshair(self, vertex_coords, cross_size):
        """Build a multi-point star/circle around a vertex position."""
        coords = []
        v = vertex_coords
        num_lines = 8  # 8 lines forming a star pattern
        for i in range(num_lines):
            angle = (2 * 3.14159 * i) / num_lines
            dx = cross_size * math.cos(angle)
            dy = cross_size * math.sin(angle)
            # Add one point on the circle and another slightly inside for a star effect
            inner = cross_size * 0.4
            dix = inner * math.cos(angle)
            diy = inner * math.sin(angle)
            if i < num_lines:
                coords.append(v + Vector((dx, dy, 0)))
                coords.append(v + Vector((dix, diy, 0)))
        # Add diameter lines
        coords.append(v + Vector((cross_size, 0, 0)))
        coords.append(v - Vector((cross_size, 0, 0)))
        coords.append(v + Vector((0, cross_size, 0)))
        coords.append(v - Vector((0, cross_size, 0)))
        coords.append(v + Vector((0, 0, cross_size)))
        coords.append(v - Vector((0, 0, cross_size)))
        return coords

    def _resolve_hover_index(self, bm, face_index, hit_location):
        """
        Return a stable index identifying the currently hovered element
        on the face at face_index. Used as part of the highlight cache
        key in vertex/edge mode so the highlight updates when the mouse
        moves to a different vertex/edge on the same face.

        Returns:
          - For vertex mode: the index of the closest vertex.
          - For edge mode: a tuple of the two vertex indices of the
            closest edge (lower, higher) so the order is stable.
          - For face mode or any failure: face_index itself (any
            change to the face re-runs the highlight).
        """
        if hit_location is None or face_index >= len(bm.faces):
            return face_index
        face = bm.faces[face_index]
        if self.selection_mode == 'vertex':
            closest = None
            closest_dist = float('inf')
            for v in face.verts:
                try:
                    d = (v.co - hit_location).length
                except ReferenceError:
                    continue
                if d < closest_dist:
                    closest_dist = d
                    closest = v
            if closest is None:
                return face_index
            return closest.index
        if self.selection_mode == 'edge':
            closest = None
            closest_dist = float('inf')
            for edge in face.edges:
                try:
                    e1, e2 = edge.verts[0].co, edge.verts[1].co
                except ReferenceError:
                    continue
                cp, _ = intersect_point_line(hit_location, e1, e2)
                if (cp - e1).dot(e2 - e1) < 0:
                    cp = e1
                elif (cp - e2).dot(e1 - e2) < 0:
                    cp = e2
                d = (hit_location - cp).length
                if d < closest_dist:
                    closest_dist = d
                    closest = edge
            if closest is None:
                return face_index
            return (min(closest.verts[0].index, closest.verts[1].index),
                    max(closest.verts[0].index, closest.verts[1].index))
        return face_index

    def _build_highlight_coords(self, context, hit_data):
        """Build vertex coords for highlighting the hovered element.

        Fast path: if the hovered face hasn't changed since the last call,
        return immediately. The highlight is already drawn for this face.
        This is the single biggest perf win for the modal — most mouse
        wiggles stay on the same face, and rebuilding the highlight (BFS
        + boundary rebuild + GPU upload) every frame was the dominant
        cost.
        """
        if hit_data is None:
            self._last_face_key = None
            self.highlight_coords.clear()
            self.highlight_drawer.disable()
            return

        # Build a stable cache key for the current hover. In face mode
        # the highlight is purely a function of the face index, so the
        # face key alone is enough. In vertex/edge mode the closest
        # vertex/edge can change even when the face stays the same,
        # so we include the resolved vertex/edge index — otherwise the
        # highlight would stay stuck on the previously-resolved vertex.
        bm = hit_data.get('bmesh_orig')
        face_index = hit_data.get('face_index')
        hit_location = hit_data.get('location')
        if bm is not None and face_index is not None:
            base_key = (id(bm), face_index)
            if self.selection_mode in ('vertex', 'edge'):
                # Resolve the closest vertex/edge BEFORE the fast-path
                # check so we can include it in the cache key.
                target_index = self._resolve_hover_index(
                    bm, face_index, hit_location)
                current_face_key = base_key + (self.selection_mode,
                                              target_index)
            else:
                current_face_key = base_key
            if current_face_key == self._last_face_key:
                return
            self._last_face_key = current_face_key
        else:
            self._last_face_key = None

        if bm is None or face_index is None or face_index >= len(bm.faces):
            self.highlight_coords.clear()
            self.highlight_drawer.disable()
            return

        face = bm.faces[face_index]
        self.highlight_coords.clear()

        if self.selection_mode == 'face':
            # Always render the coplanar face-group outline + cyan fill
            # so the user sees the full face under hover. When a circle
            # is detected, the loop_drawer (yellow) is drawn on top by
            # draw_edges / _detect_circle_loop — the 20% alpha cyan fill
            # lets the yellow circle edges read clearly through it.
            # Coplanar-connected faces boundary highlight with 0.5° tolerance
            cache_key = None
            if hit_data and 'object' in hit_data and hit_data['object']:
                cache_key = (hit_data['object'].name, face_index, 0.5)
            # Ask for triangulation too — the result is cached on
            # self._coplanar_cache so the next hover on the same face is
            # instant (O(1) lookup, no bmesh/temp-object round trip).
            face_set, boundary_edges, edge_face_count, tri_coords = self._get_coplanar_face_group(
                bm, face_index, angle_tolerance_deg=0.5, cache_key=cache_key,
                triangulate=True, context=context)
            # Outline: boundary edges of the coplanar group
            for item in boundary_edges:
                v1, v2 = item
                self.highlight_coords.append(v1.copy())
                self.highlight_coords.append(v2.copy())
            self.highlight_drawer.update_line_colour(self.colour_highlight)
            self.highlight_drawer.update_line_width(5.0)
            self.highlight_drawer.update_vertex_coords(self.highlight_coords)
            self.highlight_drawer.enable()

            # Face fill: every face in the coplanar group, drawn as
            # semi-transparent triangles in the same cyan as the outline.
            # The triangulation is produced by bpy.ops.mesh.quads_convert_to_tris
            # via _get_coplanar_face_group(triangulate=True). The triangulated
            # coordinates are cached on self._coplanar_cache so the same face
            # hover reuses them across frames.
            self.face_fill_coords.clear()
            self.face_fill_coords.extend(tri_coords)
            if self.face_fill_coords:
                self.face_fill_drawer.update_line_colour(self.colour_highlight_face)
                self.face_fill_drawer.update_vertex_coords(self.face_fill_coords)
                self.face_fill_drawer.enable()
            else:
                self.face_fill_drawer.disable()
            # If a circle loop is currently visible on this face, re-raise
            # it so the yellow circle edges draw AFTER the cyan face fill
            # (Blender draws POST_VIEW handlers in registration order).
            if (getattr(self, 'circle_centre', None) is not None
                    and hasattr(self, 'loop_drawer')
                    and self.loop_drawer.vertex_coords):
                self.loop_drawer.raise_to_front()

        elif self.selection_mode == 'edge':
            # Highlight the closest edge
            closest_edge = None
            closest_dist = float('inf')
            for edge in face.edges:
                e1 = edge.verts[0].co
                e2 = edge.verts[1].co
                cp, _ = intersect_point_line(hit_location, e1, e2)
                if (cp - e1).dot(e2 - e1) < 0:
                    cp = e1
                elif (cp - e2).dot(e1 - e2) < 0:
                    cp = e2
                dist = (hit_location - cp).length
                if dist < closest_dist:
                    closest_dist = dist
                    closest_edge = edge

            if closest_edge:
                self.highlight_coords.append(closest_edge.verts[0].co.copy())
                self.highlight_coords.append(closest_edge.verts[1].co.copy())
            self.highlight_drawer.update_line_colour(self.colour_highlight)
            self.highlight_drawer.update_line_width(6.0)
            self.highlight_drawer.update_vertex_coords(self.highlight_coords)
            self.highlight_drawer.enable()

        elif self.selection_mode == 'vertex':
            # Highlight the closest vertex with a prominent marker
            closest_vert = None
            closest_dist = float('inf')
            for vert in face.verts:
                dist = (vert.co - hit_location).length
                if dist < closest_dist:
                    closest_dist = dist
                    closest_vert = vert

            if closest_vert:
                self.vertex_marker_size = 2.0
                self.highlight_coords = self._build_vertex_crosshair(closest_vert.co.copy(), self.vertex_marker_size)
            self.highlight_drawer.update_line_colour(self.colour_highlight)
            self.highlight_drawer.update_line_width(6.0)
            self.highlight_drawer.update_vertex_coords(self.highlight_coords)
            self.highlight_drawer.enable()

        # Update text position near cursor
        if context and hasattr(context, 'region'):
            self._update_hit_text_position(context, hit_data)

    def _update_hit_text_position(self, context, hit_data):
        """Update text position based on cursor location."""
        if hasattr(self, 'tag_drawer') and self.tag_drawer:
            # Calculate screen position for the hit point
            hit_location = hit_data.get('location')
            if hit_location and context.region and context.region_data:
                screen_pos = view3d_utils.location_3d_to_region_2d(
                    context.region, context.region_data, hit_location)
                if screen_pos:
                    self.tag_drawer.update_text_pos(
                        int(screen_pos.x + 105), int(screen_pos.y - 0))

    def _set_selected_highlight(self, context, hit_data):
        """Append a persistent highlight for a selected element (accumulates with previous selections)."""
        _ = context  # accepted for symmetry with _build_highlight_coords; used by the triangulation body
        new_coords = []
        bm = hit_data.get('bmesh_orig')
        face_index = hit_data.get('face_index')
        hit_location = hit_data.get('location')
        if bm is None or face_index is None or face_index >= len(bm.faces):
            return
        face = bm.faces[face_index]

        if self.selection_mode == 'face':
            # Two UX modes depending on whether a circle was detected:
            #   - Circle detected: highlight ONLY the circle loop in
            #     orange. No coplanar-group boundary, no translucent
            #     fill — the yellow circle on hover becomes the
            #     persistent orange circle on click, matching what the
            #     user sees and expects.
            #   - No circle: highlight the coplanar face-group
            #     boundary + translucent orange fill so the user sees
            #     the full face they selected.
            has_circle = (
                getattr(self, 'circle_centre', None) is not None
                and hasattr(self, 'loop_drawer')
                and self.loop_drawer.vertex_coords
            )
            if has_circle:
                # Copy the detected circle's vertex coords straight from
                # loop_drawer (same data the yellow circle draws from)
                # and convert pairs into edge segments. Close the loop
                # with a final segment from last -> first if not already
                # closed.
                circle_coords = list(self.loop_drawer.vertex_coords)
                for i in range(len(circle_coords) - 1):
                    new_coords.append(Vector(circle_coords[i]))
                    new_coords.append(Vector(circle_coords[i + 1]))
                first = circle_coords[0]
                last = circle_coords[-1]
                if (first - last).length > 1e-6:
                    new_coords.append(Vector(last))
                    new_coords.append(Vector(first))
                # No coplanar-group fill — only the circle gets orange.
            else:
                # No circle detected: highlight the full coplanar
                # face-group boundary + translucent orange fill so the
                # user sees the face they selected.
                cache_key = None
                if hit_data and 'object' in hit_data and hit_data['object']:
                    cache_key = (hit_data['object'].name, face_index, 0.5)
                face_set, boundary_edges, edge_face_count, tri_coords = \
                    self._get_coplanar_face_group(
                        bm, face_index, angle_tolerance_deg=0.5,
                        cache_key=cache_key,
                        triangulate=True, context=context)
                for item in boundary_edges:
                    v1, v2 = item
                    new_coords.append(v1.copy())
                    new_coords.append(v2.copy())
                # Accumulate the orange fill coords. We extend the
                # drawer list so each click adds its own fill region.
                self._selected_fill_coords.extend(tri_coords)
        elif self.selection_mode == 'edge':
            # If a circle was detected AND we're in distance mode, use the
            # circle's vertex coords directly so the orange highlight
            # matches the yellow circle the user is looking at. In angle
            # mode the highlight is always the single closest edge —
            # angle mode picks one edge for the angle measurement, so
            # drawing the whole detected circle is misleading (the angle
            # isn't measured across the circle).
            circle_loop_coords = None
            if self.mode != 'angle':
                if (getattr(self, 'circle_centre', None) is not None
                        and hasattr(self, 'loop_drawer')
                        and self.loop_drawer.vertex_coords):
                    circle_loop_coords = list(self.loop_drawer.vertex_coords)
            if circle_loop_coords:
                # loop_drawer stores an ordered list of Vector positions
                # forming the circle. Convert pairs into edge segments.
                for i in range(len(circle_loop_coords) - 1):
                    new_coords.append(Vector(circle_loop_coords[i]))
                    new_coords.append(Vector(circle_loop_coords[i + 1]))
                # Close the loop if it isn't already (last -> first).
                first = circle_loop_coords[0]
                last = circle_loop_coords[-1]
                if (first - last).length > 1e-6:
                    new_coords.append(Vector(last))
                    new_coords.append(Vector(first))
            else:
                # No circle detected (or angle mode). The user picks a
                # single edge — whether for the distance measure or
                # the angle measure — so only that one edge is
                # highlighted. Walking the full connected edge loop
                # draws a confusing zig-zag pattern on quad-grid
                # meshes (each face's edges form their own loop), and
                # the angle mode UX is always single-edge anyway.
                _force_face = bool(hit_data.get('_force_face'))
                closest_edge = None
                closest_dist = float('inf')
                for edge in face.edges:
                    e1, e2 = edge.verts[0].co, edge.verts[1].co
                    cp, _ = intersect_point_line(hit_location, e1, e2)
                    if (cp - e1).dot(e2 - e1) < 0:
                        cp = e1
                    elif (cp - e2).dot(e1 - e2) < 0:
                        cp = e2
                    dist = (hit_location - cp).length
                    if dist < closest_dist:
                        closest_dist = dist
                        closest_edge = edge
                if closest_edge:
                    new_coords.append(closest_edge.verts[0].co.copy())
                    new_coords.append(closest_edge.verts[1].co.copy())
        elif self.selection_mode == 'vertex':
            closest_vert = None
            closest_dist = float('inf')
            for vert in face.verts:
                dist = (vert.co - hit_location).length
                if dist < closest_dist:
                    closest_dist = dist
                    closest_vert = vert
            if closest_vert:
                self.vertex_marker_size = 0.02
                new_coords = self._build_vertex_crosshair(closest_vert.co.copy(), self.vertex_marker_size)

        # Append to existing accumulated coords
        self._selected_coords.extend(new_coords)
        self.selected_highlight_drawer.update_line_colour(self.colour_measure_point)
        self.selected_highlight_drawer.update_line_width(6.0)
        self.selected_highlight_drawer.update_vertex_coords(self._selected_coords)
        self.selected_highlight_drawer.enable()
        # Enable the persistent orange face fill (TRIS drawer) if any
        # triangulated coords exist. The drawer is disabled when the
        # user clears all selections.
        if self._selected_fill_coords:
            self.selected_fill_drawer.update_line_colour(
                self.colour_measure_point_face)
            self.selected_fill_drawer.update_vertex_coords(
                self._selected_fill_coords)
            self.selected_fill_drawer.enable()

    def _clear_selected_highlight(self):
        """Clear the persistent selected highlight.

        Pushes empty vertex coords to every drawer before disabling, so
        if the drawer is later re-enabled without an explicit
        update_vertex_coords call it has no stale geometry to render.
        disable() only removes the draw callback — it doesn't clear the
        drawer's stored vertex_coords list, which can otherwise flash
        back during mode switches or second clicks.
        """
        self._selected_coords.clear()
        self.selected_highlight_drawer.update_vertex_coords([])
        self.selected_highlight_drawer.disable()
        if hasattr(self, '_selected_fill_coords'):
            self._selected_fill_coords.clear()
        if hasattr(self, 'selected_fill_drawer'):
            self.selected_fill_drawer.update_vertex_coords([])
            self.selected_fill_drawer.disable()

    def _clear_highlight(self):
        """Disable the highlight drawer and the face-fill overlay.

        Pushes empty vertex coords before disabling so the GPU buffer
        is empty if the drawer is later re-enabled. See
        _clear_selected_highlight for the rationale.
        """
        self.highlight_coords.clear()
        self.highlight_drawer.update_vertex_coords([])
        self.highlight_drawer.disable()
        if hasattr(self, 'face_fill_drawer'):
            self.face_fill_coords.clear()
            self.face_fill_drawer.update_vertex_coords([])
            self.face_fill_drawer.disable()
        # Reset face key so the next hover triggers a full rebuild
        # (called on mode/selection switches where the previous
        # highlight data is no longer valid)
        self._last_face_key = None

    def test_tool_header(self, context, mouse_x, mouse_y):
        """Check if mouse is in the viewport header region."""
        for area in context.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for region in area.regions:
                if region.type == "HEADER":
                    if (mouse_x >= region.x and mouse_y >= region.y
                            and mouse_x < region.width + region.x
                            and mouse_y < region.height + region.y):
                        return True
        return False

    # ──────────────── MEASUREMENT LINE DRAWING ────────────────

    def _update_measurement_line(self, point_a, point_b, context=None):
        """Draw a line from point_a to point_b with measurement text at midpoint."""
        if point_a is None or point_b is None:
            self.measure_line_coords.clear()
            self.measure_line_drawer.disable()
            # self.measure_text_drawer.disable()
            return

        self.measure_line_coords = [point_a.copy(), point_b.copy()]
        self.measure_line_drawer.update_vertex_coords(self.measure_line_coords)
        self.measure_line_drawer.enable()

        # Calculate midpoint and set text to follow it in 3D space
        mid_point = (point_a + point_b) / 2
        self.measure_text_drawer.set_text_world_pos(mid_point, offset_x=0, offset_y=20)

        # Update text based on mode
        if self.mode == 'distance':
            dist = (point_a - point_b).length
            self.measure_text_drawer.update_text(self._format_distance(dist))
            self.measure_text_drawer.enable()
        elif self.mode == 'angle' and self.first_element:
            # Angle text is handled separately
            pass

    def _update_angle_display(self, context, point_a, center_a, dir_a, point_b, center_b, dir_b):
        """Display angle measurement between two elements."""
        # Calculate angle between directions
        angle_rad = dir_a.angle(dir_b)
        angle_deg = math.degrees(angle_rad)

        # Draw lines from each center point
        line_coords = [center_a.copy(), center_b.copy()]
        self.measure_line_drawer.update_vertex_coords(line_coords)
        self.measure_line_drawer.enable()

        # Position text at midpoint of the two HIT LOCATIONS (which
        # lie on the mesh surface) rather than the centroids (which
        # can be inside the mesh for non-planar faces). The text is
        # always visible from outside the mesh when placed on the
        # surface.
        if point_a is not None and point_b is not None:
            mid_point = (point_a + point_b) / 2
        else:
            mid_point = (center_a + center_b) / 2
        self.measure_text_drawer.set_text_world_pos(mid_point, offset_x=0, offset_y=20)

        self.measure_text_drawer.update_text(self._format_angle(angle_deg))
        self.measure_text_drawer.enable()

    def _clear_measurement(self):
        """Clear all measurement overlays.

        Pushes empty vertex coords to every drawer before disabling, so
        stale geometry can't be re-rendered if a drawer is later
        re-enabled without an explicit update_vertex_coords.
        """
        self.measure_line_coords.clear()
        self.measure_line_drawer.update_vertex_coords([])
        self.measure_line_drawer.disable()
        self.measure_text_drawer.disable()
        self._clear_selected_highlight()
        self._clear_axis_lines()
        self.measurement_phase = 'idle'
        self.first_element = None
        self._second_element = None

    def _update_axis_lines(self, point_a, point_b):
        """
        Draw red (X), green (Y), and blue (Z) axis component lines between two points.
        Each axis line goes from point_a to the projection of point_b along that axis.

        Also populates the xyz_overlay_drawer — a screen-space widget
        anchored to the lower-left corner of the viewport that lists the
        same X/Y/Z deltas in a fixed font-size readout, with one row per
        axis in its axis colour. This makes the deltas legible without
        needing to read them off the small 3D-world text labels.
        """
        if point_a is None or point_b is None:
            self._clear_axis_lines()
            return

        # Copy inputs to avoid stale bmesh references
        a = point_a.copy()
        b = point_b.copy()

        # X component: (point_b.x, point_a.y, point_a.z)
        x_end = Vector((b.x, a.y, a.z))
        # Y component: (point_a.x, point_b.y, point_a.z)
        y_end = Vector((a.x, b.y, a.z))
        # Z component: (point_a.x, point_a.y, point_b.z)
        z_end = Vector((a.x, a.y, b.z))

        # X line (red)
        self.axis_x_coords = [a, x_end]
        self.axis_x_drawer.update_vertex_coords(self.axis_x_coords)
        self.axis_x_drawer.update_text("X: " + self._format_distance(abs(b.x - a.x)))
        self.axis_x_drawer.set_text_world_pos((a + x_end) / 2, offset_x=0, offset_y=12)
        self.axis_x_drawer.enable()

        # Y line (green)
        self.axis_y_coords = [a, y_end]
        self.axis_y_drawer.update_vertex_coords(self.axis_y_coords)
        self.axis_y_drawer.update_text("Y: " + self._format_distance(abs(b.y - a.y)))
        self.axis_y_drawer.set_text_world_pos((a + y_end) / 2, offset_x=0, offset_y=12)
        self.axis_y_drawer.enable()

        # Z line (blue)
        self.axis_z_coords = [a, z_end]
        self.axis_z_drawer.update_vertex_coords(self.axis_z_coords)
        self.axis_z_drawer.update_text("Z: " + self._format_distance(abs(b.z - a.z)))
        self.axis_z_drawer.set_text_world_pos((a + z_end) / 2, offset_x=0, offset_y=12)
        self.axis_z_drawer.enable()

        # Screen-space data widget — easy-to-read readout anchored to
        # the lower-left corner of the viewport. Total distance first
        # (white), then per-axis deltas in their axis colours, then the
        # last-clicked circle diameter in yellow at the bottom.
        total = (a - b).length
        x_text = "X: " + self._format_distance(abs(b.x - a.x))
        y_text = "Y: " + self._format_distance(abs(b.y - a.y))
        z_text = "Z: " + self._format_distance(abs(b.z - a.z))
        total_text = "Lin: " + self._format_distance(total)
        overlay_lines = [
            (total_text, self.colour_measure_text),
            (x_text, self.colour_axis_x),
            (y_text, self.colour_axis_y),
            (z_text, self.colour_axis_z),
        ]
        # Yellow circle-diameter row at the bottom, if a circle was
        # clicked as either endpoint of the current measurement.
        last_dia = getattr(self, '_last_circle_dia', '')
        if last_dia:
            overlay_lines.append(
                (f"Ø: {last_dia}", self.colour_measure_line))
        self.xyz_overlay_drawer.set_screen_overlay_lines(overlay_lines)
        self.xyz_overlay_drawer.enable()

    def _show_default_xyz_overlay(self, circle_dia=""):
        """Populate the lower-left XYZ data widget with 0.0 placeholders
        so the box is always visible (even when there's no active
        measurement). Called on tool start and when a measurement is
        cleared / no endpoints are available.

        If circle_dia is non-empty, an extra yellow "Ø: ..." row is
        appended — used to display the diameter of the most recently
        clicked circle while the user is still placing the second point.
        """
        placeholder_lines = [
            ("Lin: " + self._format_distance(0.0), self.colour_measure_text),
            ("X: " + self._format_distance(0.0), self.colour_axis_x),
            ("Y: " + self._format_distance(0.0), self.colour_axis_y),
            ("Z: " + self._format_distance(0.0), self.colour_axis_z),
        ]
        if circle_dia:
            placeholder_lines.append((f"Ø: {circle_dia}", self.colour_measure_line))
        if hasattr(self, 'xyz_overlay_drawer'):
            self.xyz_overlay_drawer.set_screen_overlay_lines(placeholder_lines)

    def _clear_axis_lines(self):
        """Clear all axis component overlay lines. The XYZ data widget
        in the lower-left is kept visible and reset to 0.0 placeholders
        so the box is always on-screen regardless of the show_xyz toggle.

        Pushes empty vertex coords to the 3D axis drawers before
        disabling so stale geometry can't be re-rendered if a drawer
        is later re-enabled without an explicit update_vertex_coords.
        Same pattern as _clear_selected_highlight / _clear_highlight.
        """
        self.axis_x_coords.clear()
        self.axis_y_coords.clear()
        self.axis_z_coords.clear()
        # 3D axis lines: only enabled when show_xyz is on. Push empty
        # coords first so a stale buffer can't flash back on re-enable.
        self.axis_x_drawer.update_vertex_coords([])
        self.axis_x_drawer.disable()
        self.axis_y_drawer.update_vertex_coords([])
        self.axis_y_drawer.disable()
        self.axis_z_drawer.update_vertex_coords([])
        self.axis_z_drawer.disable()
        # Show 0.0 placeholders so the widget is never blank.
        self._show_default_xyz_overlay()

    # ──────────────── RAY / HIT DETECTION ────────────────

    def get_ray_from_mouse(self, context, event):
        """Get the ray origin and direction from the current mouse position."""
        viewport_region = context.region
        viewport_region_data = context.region_data
        mouse_position = (event.mouse_region_x, event.mouse_region_y)

        ray_origin = view3d_utils.region_2d_to_origin_3d(viewport_region, viewport_region_data, mouse_position)
        ray_direction = view3d_utils.region_2d_to_vector_3d(viewport_region, viewport_region_data, mouse_position)
        ray_direction.normalize()
        return ray_origin, ray_direction

    def get_object_hit(self, context, event):
        """
        Unified hit detection. Uses circle detection (BVH ray-cast + edge loop analysis)
        for all modes when possible. Falls back to scene ray-cast + bmesh for distance/angle.
        Returns dict with: object, location, normal, face_index, bmesh_orig
        (original-topology bmesh; used for face/edge/vertex modes and circle
        detection). bmesh_tri (triangulated) and tri_face_index are also
        included for advanced use cases.
        """
        ray_origin, ray_direction = self.get_ray_from_mouse(context, event)

        # Always try circle detection first (BVH-based)
        bvh_hit = self._get_circle_hit(context, event, ray_origin, ray_direction)

        if bvh_hit is not None:
            return bvh_hit

        # Fall back to general hit detection for distance/angle modes
        return self._get_general_hit(context, event, ray_origin, ray_direction)

    def _get_circle_hit(self, context, event, ray_origin, ray_direction):
        """
        BVH-based hit detection with a persistent per-object pre-processed
        cache. On first hover of an object, it's pre-processed once and
        the resulting BVH/bmesh is cached by object name. Subsequent hovers
        on the same object reuse the cached BVH — no rebuilding, no
        re-triangulation. The expensive pre-processing pays for itself
        after the first hover on each new object.

        When the originating event has shift=True, this method returns
        None immediately so the caller's fallback (general hit detection)
        is used. This is a belt-and-braces bypass — even if the click
        branch fails to detect shift for any reason, callers using
        get_object_hit still get a non-circle hit when shift is held.
        """
        if getattr(event, 'shift', False):
            return None

        # Quick scene ray-cast to determine which object is under the cursor
        t_scene = time.time()
        depsgraph = context.view_layer.depsgraph
        scene_hit, face_location, face_normal, face_index, face_object, face_matrix = \
            context.scene.ray_cast(depsgraph, ray_origin, ray_direction)
        scene_ray_ms = (time.time() - t_scene) * 1000

        if not scene_hit:
            return None
        depsgraph = context.view_layer.depsgraph
        scene_hit, face_location, face_normal, face_index, face_object, face_matrix = \
            context.scene.ray_cast(depsgraph, ray_origin, ray_direction)

        if not scene_hit:
            return None

        # Skip debug objects
        if face_object.name.startswith("_debug_face_group_"):
            return None

        # Filter out any deleted objects from hidden list
        valid_hidden = []
        for o in self.objects_hidden:
            try:
                if o.name in bpy.data.objects:
                    valid_hidden.append(o)
            except ReferenceError:
                pass
        self.objects_hidden = valid_hidden

        if face_object.name not in self.objects_visible:
            if face_object.name not in [o.name for o in self.objects_hidden]:
                self.objects_hidden.append(face_object)
            face_object.hide_set(True)
            return None

        if face_object.type != 'MESH':
            return None

        obj_name = face_object.name

        # Force rebuild if requested (e.g. user toggled a setting)
        if self.next_object and obj_name in _BVH_OBJECT_CACHE:
            self._free_bvh_cache_entry(obj_name)
        self.next_object = False

        # Build or reuse the preprocessed BVH for this object.
        # _BVH_OBJECT_CACHE survives across MOUSEMOVE events within a
        # single modal session, and is cleared on tool exit (RMB/ESC).
        # When the object has been edited, the fingerprint check below
        # detects it and we transparently rebuild.
        t_fp = time.time()
        current_fp = bvh_ray.compute_fingerprint(face_object)
        fp_ms = (time.time() - t_fp) * 1000
        was_stale = False
        if current_fp is not None and obj_name in _BVH_OBJECT_CACHE:
            cached_ray, cached_fp = _BVH_OBJECT_CACHE[obj_name]
            if cached_fp == current_fp:
                # Cached entry is still valid — instant HIT
                self.bvh_data = cached_ray
                t0 = time.time()
                bvh_hit = self.bvh_data.ray_cast_bvh(ray_origin, ray_direction)
                return bvh_hit
            else:
                # Object changed since last build — drop the stale entry
                was_stale = True
                self._free_bvh_cache_entry(obj_name)

        # MISS (never seen this object) or REBUILD (object changed).
        # Both paths do the same work.
        result = bvh_ray.preprocess_object_for_raycast(context, face_object)
        if result is None:
            return None
        # Result is (bvh, bmesh) — single bmesh, no triangulated copy,
        # no index map. BVHTree.ray_cast's face_index already refers to
        # the correct face in this bmesh.
        bvh, bm = result
        bvh_ray_instance = bvh_ray.BVHRay.from_preprocessed(
            face_object, bvh, bm)
        # If we couldn't compute a fingerprint earlier, get one now so
        # the entry can be validated on future hovers.
        if current_fp is None:
            current_fp = bvh_ray.compute_fingerprint(face_object)
        _BVH_OBJECT_CACHE[obj_name] = (bvh_ray_instance, current_fp)
        self.bvh_data = bvh_ray_instance
        bvh_hit = self.bvh_data.ray_cast_bvh(ray_origin, ray_direction)
        return bvh_hit

    def _free_bvh_cache_entry(self, obj_name):
        """
        Free the BVH/bmesh for a single cached object. Called when an
        entry needs to be evicted (e.g. force-rebuild, fingerprint
        mismatch, or on tool exit).
        """
        entry = _BVH_OBJECT_CACHE.pop(obj_name, None)
        if entry is None:
            return
        # entry is a (bvh_ray_instance, fingerprint) tuple
        bvh_ray_instance = entry[0]
        for data in bvh_ray_instance.bvh_data.values():
            # data = [bvh, bmesh]
            # Free the bmesh (the BVH is a managed C object with no
            # Python free, so the hasattr check below skips it cleanly).
            for item in data[1:]:
                if item is not None and hasattr(item, 'free'):
                    try:
                        item.free()
                    except Exception:
                        pass

    def _get_general_hit(self, context, event, ray_origin, ray_direction):
        """General hit detection for distance/angle modes using scene ray-cast + bmesh."""
        depsgraph = context.view_layer.depsgraph
        scene_hit, face_location, face_normal, face_index, face_object, face_matrix = \
            context.scene.ray_cast(depsgraph, ray_origin, ray_direction)

        if not scene_hit or face_object.type != 'MESH':
            return None

        if face_object.name not in self.objects_visible:
            self.objects_hidden.append(face_object)
            face_object.hide_set(True)
            return None

        # Build or retrieve a cached bmesh for this object
        bm_orig = self._get_bmesh_for_object(context, face_object)
        if bm_orig is None:
            return None

        return {
            "object": face_object,
            "bmesh_orig": bm_orig,
            "bmesh_ray_scan": None,
            "face_index": face_index,
            "location": face_location,
            "normal": face_normal,
            "distance": (ray_origin - face_location).length,
        }

    def _get_bmesh_for_object(self, context, obj):
        """Get or create a world-space bmesh for the given object."""
        # Use a simple cache stored on self
        if not hasattr(self, '_bmesh_cache'):
            self._bmesh_cache = {}

        if obj.name in self._bmesh_cache:
            return self._bmesh_cache[obj.name]

        if obj.data.is_editmode:
            obj.update_from_editmode()
            bm = bmesh.new()
            bm.from_mesh(obj.data)
        else:
            depsgraph = context.evaluated_depsgraph_get()
            eval_obj = obj.evaluated_get(depsgraph)
            bm = bmesh.new()
            bm.from_mesh(eval_obj.data)

        bm.transform(obj.matrix_world)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.verts.index_update()
        bm.edges.index_update()
        bm.faces.index_update()

        self._bmesh_cache[obj.name] = bm
        return bm

    # ──────────────── MODAL HANDLER ────────────────

    def modal(self, context, event):
        """Modal handler for the measure tool."""
        # ── View matrix comparison for rotation detection ──
        if context.region_data:
            current_matrix = context.region_data.view_matrix.copy()
            if self._prev_view_matrix is not None:
                if current_matrix != self._prev_view_matrix:
                    # View is being manipulated (rotating/panning/zooming)
                    self.rotating_view = True
                    # Tolerance text follows screen cursor — hide during rotation
                    self.path_tol_drawer.text = ""
                    self.circle_tol_drawer.text = ""

                    self._prev_view_matrix = current_matrix
                    # If this event is a mouse click (not just MOUSEMOVE), the user has
                    # finished rotating and wants to interact - handle it normally
                    if event.type in {"LEFTMOUSE", "RIGHTMOUSE", "ESC"} and event.value == "PRESS":
                        self.rotating_view = False
                    else:
                        return {"PASS_THROUGH"}
                else:
                    # View is stable - immediately allow normal processing
                    if self.rotating_view:
                        self.rotating_view = False
                        # Restore tolerance text
                        self.path_tol_drawer.text = "Path Tol: " + str(round(self.path_tolerance, 2))
                        self.circle_tol_drawer.text = "Circle Tol: " + str(round(self.circle_tolerance, 2))
                        context.area.tag_redraw()
                    self._prev_view_matrix = current_matrix
            else:
                # No region data available, skip detection
                self.rotating_view = False

        # ── If rotating view, disallow all other functionality ──
        if self.rotating_view:
            return {"PASS_THROUGH"}

        # Sync operator state with scene properties (header dropdowns)
        props = context.scene.caliper_props
        old_mode = self.mode
        old_selection = self.selection_mode
        self.mode = props.measure_mode
        self.selection_mode = props.selection_mode

        # Handle XYZ toggle: show/hide the 3D axis lines on any existing
        # completed measurement. The lower-left data widget stays visible
        # in all states (populated by _update_axis_lines or by the 0.0
        # placeholder when no measurement is active).
        if hasattr(self, '_last_show_xyz') and self._last_show_xyz != props.show_xyz:
            if self.measurement_phase == 'complete' and self.first_element:
                if props.show_xyz:
                    if len(self.measure_line_coords) >= 2:
                        a = self.measure_line_coords[0]
                        b = self.measure_line_coords[1]
                        self._update_axis_lines(a, b)
                else:
                    # Hide only the 3D axis lines. The widget box is left
                    # visible with its current deltas (or 0.0 if no
                    # measurement is active — that's handled by _clear_measurement
                    # / invoke, not here).
                    self.axis_x_drawer.disable()
                    self.axis_y_drawer.disable()
                    self.axis_z_drawer.disable()
                context.area.tag_redraw()
        self._last_show_xyz = props.show_xyz

        # Trigger mode switch cleanup if anything changed
        if self.mode != old_mode or self.selection_mode != old_selection:
            if self.mode == 'distance':
                self._clear_measurement()
                self._clear_highlight()
                self._clear_selected_highlight()
                self.tag_drawer.text = ""
                self.tag_drawer.disable()
                self.path_tol_drawer.enable()
                self.circle_tol_drawer.enable()
            elif self.mode == 'angle':
                if self.selection_mode == 'vertex':
                    self.selection_mode = 'face'
                    props.selection_mode = 'face'
                self._clear_measurement()
                self._clear_highlight()
                # Defensive: also wipe the selected_highlight drawer's
                # coords and clear the loop_drawer coords so leftover
                # geometry from a prior mode can't re-render.
                self._clear_selected_highlight()
                if hasattr(self, 'loop_drawer'):
                    self.loop_drawer.update_vertex_coords([])
                    self.loop_drawer.disable()
                self.tag_drawer.text = ""
                self.tag_drawer.disable()
                self.path_tol_drawer.disable()
                self.circle_tol_drawer.disable()
            self._update_status_text(context)
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}

        # ── Header region handling (allow UI button clicks) ──
        if self.test_tool_header(context, event.mouse_x, event.mouse_y):
            if event.type in {"MOUSEMOVE"}:
                context.window.cursor_set("DEFAULT")
                return {"PASS_THROUGH"}
            elif event.type == "LEFTMOUSE":
                if event.value in {"PRESS", "RELEASE"}:
                    return {"PASS_THROUGH"}

        # ── Key presses for mode/selection switching ──
        if event.type == 'D' and event.value == 'PRESS':
            self.mode = 'distance'
            context.scene.caliper_props.measure_mode = 'distance'
            self._clear_measurement()
            self._clear_highlight()
            self._clear_selected_highlight()
            if hasattr(self, 'loop_drawer'):
                self.loop_drawer.update_vertex_coords([])
                self.loop_drawer.disable()
            self.tag_drawer.text = ""
            self.tag_drawer.disable()
            self.path_tol_drawer.enable()
            self.circle_tol_drawer.enable()
            self._update_status_text(context)
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}

        if event.type == 'A' and event.value == 'PRESS':
            self.mode = 'angle'
            context.scene.caliper_props.measure_mode = 'angle'
            if self.selection_mode == 'vertex':
                self.selection_mode = 'face'
                context.scene.caliper_props.selection_mode = 'face'
            self._clear_measurement()
            self._clear_highlight()
            self._clear_selected_highlight()
            if hasattr(self, 'loop_drawer'):
                self.loop_drawer.update_vertex_coords([])
                self.loop_drawer.disable()
            self.tag_drawer.text = ""
            self.tag_drawer.disable()
            self.path_tol_drawer.disable()
            self.circle_tol_drawer.disable()
            self._update_status_text(context)
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}

        if event.type == 'ONE' and event.value == 'PRESS':
            if not (self.mode == 'angle' and self.selection_mode == 'vertex'):
                self.selection_mode = 'face'
                context.scene.caliper_props.selection_mode = 'face'
                self._clear_measurement()
                self._clear_highlight()
                self._update_status_text(context)
                context.area.tag_redraw()
            return {"RUNNING_MODAL"}

        if event.type == 'TWO' and event.value == 'PRESS':
            self.selection_mode = 'edge'
            context.scene.caliper_props.selection_mode = 'edge'
            self._clear_measurement()
            self._clear_highlight()
            self._update_status_text(context)
            context.area.tag_redraw()
            return {"RUNNING_MODAL"}

        if event.type == 'THREE' and event.value == 'PRESS':
            if self.mode != 'angle':
                self.selection_mode = 'vertex'
                context.scene.caliper_props.selection_mode = 'vertex'
                self._clear_measurement()
                self._clear_highlight()
                self._update_status_text(context)
                context.area.tag_redraw()
            return {"RUNNING_MODAL"}

        # ── Mouse / Navigation ──
        if event.type == "MOUSEMOVE":
            if event.mouse_prev_x != event.mouse_x or event.mouse_prev_y != event.mouse_y:
                if not self.test_tool_header(context, event.mouse_x, event.mouse_y):
                    context.window.cursor_set("PICK_AREA")
                # Only update screen-following positions (tolerance text uses screen coords)
                self.path_tol_drawer.update_text_pos(event.mouse_region_x + 80, event.mouse_region_y - 35)
                self.circle_tol_drawer.update_text_pos(event.mouse_region_x + 83, event.mouse_region_y - 65)

                if self.mode == 'distance':
                    hit_data = self.get_object_hit(context, event)
                    self.current_hit_data = hit_data
                    if hit_data is not None:
                        # Circle detection runs in face + edge mode — the
                        # SHIFT key dynamically disables it (see
                        # draw_edges early-return on event.shift). Vertex
                        # mode picks the vertex directly with no circle
                        # detection — it doesn't make sense to snap to a
                        # detected circle when the user is picking a
                        # specific vertex.
                        if self.selection_mode in ('face', 'edge'):
                            t_draw = time.time()
                            self.draw_edges(context, hit_data, event)
                            draw_ms = (time.time() - t_draw) * 1000
                            has_circle = (hasattr(self, 'circle_centre')
                                          and self.circle_centre is not None)
                            snap_point = (self.circle_centre
                                          if has_circle
                                          else self._compute_snap_point(hit_data))
                        else:
                            draw_ms = 0.0
                            self.circle_centre = None
                            self.circle_dia = ""
                            self._last_circle_dia = ""
                            if hasattr(self, 'loop_drawer'):
                                self.loop_drawer.disable()
                            snap_point = self._compute_snap_point(hit_data)
                        if snap_point:
                            t_hi = time.time()
                            self._build_highlight_coords(context, hit_data)
                            hi_ms = (time.time() - t_hi) * 1000
                            if self.measurement_phase == 'first_selected' and self.first_element:
                                # Draw dynamic line from first point to current snap point
                                self._update_measurement_line(
                                    self.first_element['point'], snap_point, context)
                            context.area.tag_redraw()
                    else:
                        self._clear_highlight()
                        # Clean up circle detection overlays (debug, loop, tag) when mouse leaves object
                        if hasattr(self, 'debug_drawer'):
                            self.debug_drawer.disable()
                        if hasattr(self, 'loop_drawer'):
                            self.loop_drawer.disable()
                        self.tag_drawer.disable()
                        # Only clear the line if we are NOT in 'complete' phase (keep completed measurements visible)
                        if self.measurement_phase != 'first_selected' and self.measurement_phase != 'complete':
                            self._update_measurement_line(None, None)
                        context.area.tag_redraw()

                elif self.mode == 'angle':
                    hit_data = self.get_object_hit(context, event)
                    self.current_hit_data = hit_data
                    if hit_data is not None:
                        self._build_highlight_coords(context, hit_data)
                        if self.measurement_phase == 'first_selected' and self.first_element:
                            # Draw dynamic angle preview from first element to current hit
                            if self.selection_mode == 'edge':
                                edge_data = self._get_edge_data(hit_data)
                                if edge_data:
                                    self._update_angle_display(
                                        context,
                                        self.first_element.get('hit_location'),
                                        self.first_element['center'],
                                        self.first_element['direction'],
                                        hit_data.get('location'),
                                        edge_data[1], edge_data[2])
                            elif self.selection_mode == 'face':
                                face_data = self._get_face_data(hit_data)
                                if face_data:
                                    self._update_angle_display(
                                        context,
                                        self.first_element.get('hit_location'),
                                        self.first_element['center'],
                                        self.first_element['direction'],
                                        hit_data.get('location'),
                                        face_data[1], face_data[2])
                        elif (self.measurement_phase == 'complete'
                              and self.first_element
                              and getattr(self, '_second_element', None)):
                            # Re-apply the finalized angle so the angle
                            # text remains visible after the mouse leaves
                            # the object and re-enters. The text midpoint
                            # is the hit_location midpoint (on the mesh
                            # surface) so the text isn't hidden inside the
                            # mesh for non-planar faces.
                            se = self._second_element
                            self._update_angle_display(
                                context,
                                self.first_element.get('hit_location'),
                                self.first_element['center'],
                                self.first_element['direction'],
                                se.get('hit_location'),
                                se['center'], se['direction'])
                        context.area.tag_redraw()
                    else:
                        # In angle mode, do NOT call _clear_highlight
                        # when a measurement is in progress — the cyan
                        # face highlight and angle text must stay visible
                        # together with the rest of the persistence
                        # overlays. Calling _clear_highlight here was
                        # implicitly disabling the measure_text_drawer
                        # (the angle text) when the mouse moved off every
                        # object, because the line/triangulation reset
                        # inside _clear_highlight propagated and the
                        # next tag_redraw wiped the text glyph.
                        if self.measurement_phase == 'idle':
                            # No measurement in progress — just clear
                            # the cyan face highlight. There's nothing
                            # else to clear (no angle text, no selection).
                            self._clear_highlight()
                        elif (self.measurement_phase == 'complete'
                              and self.first_element
                              and getattr(self, '_second_element', None)):
                            # Re-apply the finalized angle so the angle
                            # text stays enabled even after the mouse
                            # leaves the object. Without this the user
                            # reported the readout disappearing.
                            se = self._second_element
                            self._update_angle_display(
                                context,
                                self.first_element.get('hit_location'),
                                self.first_element['center'],
                                self.first_element['direction'],
                                se.get('hit_location'),
                                se['center'], se['direction'])
                        else:
                            # 'first_selected' phase — clear only the
                            # dynamic preview, keep the first element's
                            # selection visible.
                            self._clear_highlight()
                        context.area.tag_redraw()

        # ── Left click ──
        if event.type in {"LEFTMOUSE"} and event.value == "PRESS":
            if not self.test_tool_header(context, event.mouse_x, event.mouse_y):
                if self.mode == 'distance':
                    # SHIFT bypasses circle detection — useful when the
                    # hovered circle is occluding the actual face you
                    # want to pick. We hit-test the general ray directly
                    # and clear any pending circle state so the click is
                    # treated as a plain face selection.
                    shift_held = bool(getattr(event, 'shift', False))
                    if shift_held:
                        ray_origin, ray_direction = self.get_ray_from_mouse(context, event)
                        hit_data = self._get_general_hit(context, event, ray_origin, ray_direction)
                        # Mark this hit_data so the downstream draw_edges
                        # call below is skipped — otherwise draw_edges
                        # re-runs circle detection and overwrites the
                        # cleared circle_centre, snapping back to the
                        # circle centre instead of the face.
                        if hit_data is not None:
                            hit_data['_force_face'] = True
                        # Wipe any circle state left over from the hover
                        # so the rest of the click flow acts on a face.
                        self.circle_centre = None
                        self.circle_dia = ""
                        self._last_circle_dia = ""
                        if hasattr(self, 'loop_drawer'):
                            self.loop_drawer.disable()
                    else:
                        hit_data = self.get_object_hit(context, event)
                    if hit_data is None:
                        # Click into blank space — reset any active
                        # selections (pending first/second element,
                        # measurement line, selected highlight, axis
                        # lines). The XYZ widget stays visible with
                        # 0.0 placeholders.
                        if (self.measurement_phase != 'idle'
                                or self.first_element is not None
                                or self._second_element is not None
                                or self.measure_line_coords
                                or (hasattr(self, '_selected_coords')
                                    and self._selected_coords)):
                            self._clear_measurement()
                            context.area.tag_redraw()
                        return {"RUNNING_MODAL"}
                    # Circle detection runs in face + edge mode. Vertex
                    # mode still picks the vertex directly. SHIFT bypass
                    # applies in both face and edge modes — when SHIFT
                    # is held, _force_face is set and draw_edges is
                    # skipped, so the snap-point is the face/edge
                    # centroid rather than the detected circle centre.
                    force_face = bool(hit_data.get('_force_face'))
                    has_circle = False
                    if self.selection_mode in ('face', 'edge') and not force_face:
                        self.draw_edges(context, hit_data, event)
                        has_circle = (hasattr(self, 'circle_centre')
                                      and self.circle_centre is not None)
                        snap_point = (self.circle_centre
                                      if has_circle
                                      else self._compute_snap_point(hit_data))
                    elif force_face:
                        # SHIFT bypass: skip draw_edges (which would
                        # re-run circle detection and overwrite the
                        # cleared circle_centre). Snap straight to the
                        # face/edge centroid.
                        snap_point = self._compute_snap_point(hit_data)
                    else:
                        self.circle_centre = None
                        self.circle_dia = ""
                        self._last_circle_dia = ""
                        if hasattr(self, 'loop_drawer'):
                            self.loop_drawer.disable()
                        # First click in edge/vertex mode is not on a circle,
                        # so the Ø row in the widget should disappear.
                        self._show_default_xyz_overlay()
                        snap_point = self._compute_snap_point(hit_data)
                    if snap_point is None:
                        return {"RUNNING_MODAL"}

                    if self.measurement_phase == 'idle':
                        # First point selected. Clear the previous
                        # measurement's 3D axis lines (red/green/blue)
                        # and reset the lower-left widget to 0.0
                        # placeholders so the old XYZ components don't
                        # linger on screen until the second click.
                        # _clear_axis_lines disables the 3D lines and
                        # calls _show_default_xyz_overlay() to reset
                        # the widget (which still stays visible).
                        self._clear_axis_lines()
                        self.first_element = {
                            'point': snap_point.copy(),
                            'type': ('circle_center' if has_circle
                                     else self.selection_mode),
                            'object': hit_data.get('object'),
                            'hit_data': hit_data,
                        }
                        # Remember the diameter of the last-clicked circle
                        # so the XYZ data widget can show it.
                        self._last_circle_dia = (
                            self.circle_dia if has_circle else '')
                        # If the first click landed on a circle, show the
                        # diameter in the lower-left widget right away
                        # (0.0 placeholders for Lin/X/Y/Z + Ø row).
                        if has_circle and self.circle_dia:
                            self._show_default_xyz_overlay(self.circle_dia)
                        self.measurement_phase = 'first_selected'
                        # Set persistent selected highlight
                        self._set_selected_highlight(context, hit_data)
                        # Clear the live face highlight so only the
                        # orange selection marker stays visible.
                        self._clear_highlight()
                        # Draw initial point marker (short crosshair)
                        self.measure_line_coords.clear()
                        cross_size = 0.005
                        marker = [
                            snap_point + Vector((cross_size, 0, 0)),
                            snap_point - Vector((cross_size, 0, 0)),
                            snap_point + Vector((0, cross_size, 0)),
                            snap_point - Vector((0, cross_size, 0)),
                        ]
                        # Use highlight drawer for the marker
                        self.highlight_coords = marker
                        self.highlight_drawer.update_vertex_coords(self.highlight_coords)
                        self.highlight_drawer.update_line_colour(self.colour_measure_point)
                        self.highlight_drawer.update_line_width(8.0)
                        self.highlight_drawer.enable()

                    elif self.measurement_phase == 'first_selected':
                        # Second point selected, finalize measurement
                        self._update_measurement_line(
                            self.first_element['point'], snap_point, context)
                        if props.show_xyz:
                            # Show 3D axis lines + the lower-left widget
                            # populated with the real deltas.
                            self._update_axis_lines(self.first_element['point'], snap_point)
                        else:
                            # Hide 3D axis lines but keep the widget visible
                            # with real values (so toggling Show XYZ back on
                            # later is instant).
                            self._clear_axis_lines()
                            if hasattr(self, 'xyz_overlay_drawer'):
                                a = self.first_element['point']
                                b = snap_point
                                total = (a - b).length
                                overlay_lines = [
                                    ("Lin: " + self._format_distance(total), self.colour_measure_text),
                                    ("X: " + self._format_distance(abs(b.x - a.x)), self.colour_axis_x),
                                    ("Y: " + self._format_distance(abs(b.y - a.y)), self.colour_axis_y),
                                    ("Z: " + self._format_distance(abs(b.z - a.z)), self.colour_axis_z),
                                ]
                                last_dia = getattr(self, '_last_circle_dia', '')
                                if last_dia:
                                    overlay_lines.append((f"Ø: {last_dia}", self.colour_measure_line))
                                self.xyz_overlay_drawer.set_screen_overlay_lines(overlay_lines)
                        self.measurement_phase = 'complete'
                        # Set persistent selected highlight for second point
                        self._set_selected_highlight(context, hit_data)
                        # Clear the live face highlight so only the
                        # orange selection marker stays visible.
                        self._clear_highlight()

                    elif self.measurement_phase == 'complete':
                        # Clear old measurement, then start new one with current click
                        self._clear_selected_highlight()
                        self.measure_line_coords.clear()
                        self.measure_line_drawer.disable()
                        self.measure_text_drawer.disable()
                        # Hide the 3D axis lines (red/green/blue) from
                        # the previous measurement. _clear_axis_lines
                        # disables the drawers AND resets the lower-left
                        # widget to 0.0 placeholders so the old XYZ
                        # readout doesn't linger on screen.
                        self._clear_axis_lines()
                        self.first_element = None
                        # Immediately start new measurement with this hit
                        self.first_element = {
                            'point': snap_point.copy(),
                            'type': 'circle_center' if has_circle else self.selection_mode,
                            'object': hit_data.get('object'),
                            'hit_data': hit_data,
                        }
                        self.measurement_phase = 'first_selected'
                        self._set_selected_highlight(context, hit_data)
                        # Clear the live face highlight so only the
                        # orange selection marker stays visible.
                        self._clear_highlight()
                        # Draw initial point marker
                        cross_size = 0.005
                        marker = [
                            snap_point + Vector((cross_size, 0, 0)),
                            snap_point - Vector((cross_size, 0, 0)),
                            snap_point + Vector((0, cross_size, 0)),
                            snap_point - Vector((0, cross_size, 0)),
                        ]
                        self.highlight_coords = marker
                        self.highlight_drawer.update_vertex_coords(self.highlight_coords)
                        self.highlight_drawer.update_line_colour(self.colour_measure_point)
                        self.highlight_drawer.update_line_width(8.0)
                        self.highlight_drawer.enable()

                elif self.mode == 'angle':
                    hit_data = self.get_object_hit(context, event)
                    if hit_data is None:
                        # Click into blank space — reset selections
                        # (same behaviour as distance mode).
                        if (self.measurement_phase != 'idle'
                                or self.first_element is not None
                                or self._second_element is not None
                                or self.measure_line_coords
                                or (hasattr(self, '_selected_coords')
                                    and self._selected_coords)):
                            self._clear_measurement()
                            context.area.tag_redraw()
                        return {"RUNNING_MODAL"}
                    if hit_data is not None and self.current_hit_data is not None:
                        if self.measurement_phase == 'idle':
                            # Clear the previous measurement's 3D axis
                            # lines + widget so old XYZ components don't
                            # linger. _clear_axis_lines also resets the
                            # lower-left widget to 0.0 placeholders.
                            self._clear_axis_lines()
                            # Clear old highlights and measurement before starting new measurement
                            self._clear_selected_highlight()
                            self.measure_line_coords.clear()
                            self.measure_line_drawer.disable()
                            self.measure_text_drawer.disable()
                            self.first_element = None
                            self._second_element = None
                            # Store first element
                            if self.selection_mode == 'edge':
                                edge_data = self._get_edge_data(hit_data)
                                if edge_data:
                                    self.first_element = {
                                        'center': edge_data[1],
                                        'direction': edge_data[2],
                                        'type': 'edge',
                                        'object': hit_data.get('object'),
                                        'hit_data': hit_data,
                                        'hit_location': hit_data.get('location'),
                                    }
                                    self.measurement_phase = 'first_selected'
                                    # Show first edge highlight
                                    self._build_highlight_coords(context, hit_data)
                            elif self.selection_mode == 'face':
                                face_data = self._get_face_data(hit_data)
                                if face_data:
                                    self.first_element = {
                                        'center': face_data[1],
                                        'direction': face_data[2],
                                        'type': 'face',
                                        'object': hit_data.get('object'),
                                        'hit_data': hit_data,
                                        'hit_location': hit_data.get('location'),
                                    }
                                    self.measurement_phase = 'first_selected'
                                    self._build_highlight_coords(context, hit_data)
                            # Set persistent selected highlight
                            self._set_selected_highlight(context, hit_data)
                            # In angle mode we KEEP the cyan face highlight
                            # so the user can see both the orange selection
                            # marker and the blue face highlight.

                        elif self.measurement_phase == 'first_selected':
                            # Second element selected, calculate angle
                            if self.selection_mode == 'edge':
                                edge_data = self._get_edge_data(hit_data)
                                if edge_data and self.first_element:
                                    self._update_angle_display(
                                        context,
                                        self.first_element.get('hit_location'),
                                        self.first_element['center'],
                                        self.first_element['direction'],
                                        hit_data.get('location'),
                                        edge_data[1], edge_data[2])
                                    self._second_element = {
                                        'center': edge_data[1],
                                        'direction': edge_data[2],
                                        'hit_location': hit_data.get('location'),
                                    }
                                    self.measurement_phase = 'complete'
                            elif self.selection_mode == 'face':
                                face_data = self._get_face_data(hit_data)
                                if face_data and self.first_element:
                                    self._update_angle_display(
                                        context,
                                        self.first_element.get('hit_location'),
                                        self.first_element['center'],
                                        self.first_element['direction'],
                                        hit_data.get('location'),
                                        face_data[1], face_data[2])
                                    self._second_element = {
                                        'center': face_data[1],
                                        'direction': face_data[2],
                                        'hit_location': hit_data.get('location'),
                                    }
                                    self.measurement_phase = 'complete'
                            # Set persistent selected highlight for second element
                            self._set_selected_highlight(context, hit_data)
                            # In angle mode we KEEP the cyan face highlight.

                        elif self.measurement_phase == 'complete':
                            # Clear old measurement, then start new one with current click
                            self._clear_selected_highlight()
                            self.measure_line_coords.clear()
                            self.measure_line_drawer.disable()
                            self.measure_text_drawer.disable()
                            self.first_element = None
                            self._second_element = None
                            # Immediately start new measurement with this hit
                            if self.selection_mode == 'edge':
                                edge_data = self._get_edge_data(hit_data)
                                if edge_data:
                                    self.first_element = {
                                        'center': edge_data[1],
                                        'direction': edge_data[2],
                                        'type': 'edge',
                                        'object': hit_data.get('object'),
                                        'hit_data': hit_data,
                                    }
                                    self.measurement_phase = 'first_selected'
                                    self._build_highlight_coords(context, hit_data)
                                    self._set_selected_highlight(context, hit_data)
                            elif self.selection_mode == 'face':
                                face_data = self._get_face_data(hit_data)
                                if face_data:
                                    self.first_element = {
                                        'center': face_data[1],
                                        'direction': face_data[2],
                                        'type': 'face',
                                        'object': hit_data.get('object'),
                                        'hit_data': hit_data,
                                    }
                                    self.measurement_phase = 'first_selected'
                                    self._build_highlight_coords(context, hit_data)
                                    self._set_selected_highlight(context, hit_data)
                    if self.measurement_phase != 'first_selected':
                        self.measurement_phase = 'idle'

                context.area.tag_redraw()

        # ── Right click / ESC to exit ──
        if event.type in {"RIGHTMOUSE", "ESC"} and event.value == "PRESS":
            for obj in self.objects_hidden:
                obj.hide_set(False)
            context.area.header_text_set(None)
            context.window.cursor_set("DEFAULT")
            self.props.path_tol = self.path_tolerance
            self.props.circle_tol = self.circle_tolerance
            self.tag_drawer.text = ""
            self.tag_drawer.disable()
            if hasattr(self, 'loop_drawer'):
                self.loop_drawer.disable()
            if hasattr(self, 'debug_drawer'):
                self.debug_drawer.disable()
            self.path_tol_drawer.disable()
            self.circle_tol_drawer.disable()
            self.highlight_drawer.disable()
            self.selected_highlight_drawer.disable()
            if hasattr(self, 'selected_fill_drawer'):
                self.selected_fill_drawer.disable()
            self.face_fill_drawer.disable()
            self.measure_line_drawer.disable()
            self.measure_text_drawer.disable()
            self._clear_axis_lines()
            # Hide the lower-left XYZ data widget on tool exit. _clear_axis_lines
            # keeps it visible during the modal (with 0.0 placeholders or real
            # deltas), but on tool end the widget must disappear.
            if hasattr(self, 'xyz_overlay_drawer'):
                self.xyz_overlay_drawer.clear_screen_overlay()
                self.xyz_overlay_drawer.disable()
            context.area.tag_redraw()
            # Clean up bmesh cache
            if hasattr(self, '_bmesh_cache'):
                for bm in self._bmesh_cache.values():
                    bm.free()
                self._bmesh_cache = {}
            # Clean up the module-level BVH cache — free every bmesh
            # in it and drop the dict. This releases memory between
            # measure-tool invocations. The next tool start rebuilds
            # from scratch (preprocess is ~300ms per object, acceptable).
            for name in list(_BVH_OBJECT_CACHE.keys()):
                self._free_bvh_cache_entry(name)
            _BVH_OBJECT_CACHE.clear()
            self.bvh_data = None
            if hasattr(self, '_coplanar_cache'):
                self._coplanar_cache.clear()
            self._last_face_key = None
            # Restore original header
            bpy.types.VIEW3D_HT_header.draw = self.old_header
            return {"FINISHED"}

        # ── Tolerance adjustment (distance mode, requires modifier keys) ──
        if event.type == "WHEELUPMOUSE" and self.mode == 'distance' and (event.ctrl or event.alt):
            if event.ctrl:
                if event.shift:
                    self.path_tolerance += 0.01
                else:
                    self.path_tolerance += 0.1
                if self.path_tolerance > 1.0:
                    self.path_tolerance = 1.0
            self.path_tol_drawer.update_text("Path Tol: " + str(round(self.path_tolerance, 2)))
            hit_data = self.get_object_hit(context, event)
            if hit_data is not None:
                self.draw_edges(context, hit_data, event)
            context.area.tag_redraw()

            if event.alt:
                if event.shift:
                    self.circle_tolerance += 0.1
                else:
                    self.circle_tolerance += 1.0
                if self.circle_tolerance > 50.0:
                    self.circle_tolerance = 50.0
            self.circle_tol_drawer.update_text("Circle Tol: " + str(round(self.circle_tolerance, 2)))
            hit_data = self.get_object_hit(context, event)
            if hit_data is not None:
                self.draw_edges(context, hit_data, event)
            context.area.tag_redraw()

            return {"RUNNING_MODAL"}

        if event.type == "WHEELDOWNMOUSE" and self.mode == 'distance' and (event.ctrl or event.alt):
            if event.ctrl:
                if event.shift:
                    self.path_tolerance -= 0.01
                else:
                    self.path_tolerance -= 0.1
                if self.path_tolerance < -1.0:
                    self.path_tolerance = -1.0
            self.path_tol_drawer.update_text("Path Tol: " + str(round(self.path_tolerance, 2)))
            hit_data = self.get_object_hit(context, event)
            if hit_data is not None:
                self.draw_edges(context, hit_data, event)
            context.area.tag_redraw()

            if event.alt:
                if event.shift:
                    self.circle_tolerance -= 0.1
                else:
                    self.circle_tolerance -= 1.0
                if self.circle_tolerance < 0.0:
                    self.circle_tolerance = 0.0
            self.circle_tol_drawer.update_text("Circle Tol: " + str(round(self.circle_tolerance, 2)))
            hit_data = self.get_object_hit(context, event)
            if hit_data is not None:
                self.draw_edges(context, hit_data, event)
            context.area.tag_redraw()

            return {"RUNNING_MODAL"}

        # ── Viewport navigation passthrough ──
        # The view matrix comparison at the top handles skipping during rotation.
        if event.type in {
            "WHEELUPMOUSE",
            "WHEELDOWNMOUSE",
            "MIDDLEMOUSE",
            "HOME",
            "NUMPAD_1",
            "NUMPAD_3",
            "NUMPAD_7",
            "NUMPAD_5",
        } and not event.alt:
            return {"PASS_THROUGH"}

        return {"RUNNING_MODAL"}

    # ════════════════════════════════════════════════════════
    # CIRCLE MEASUREMENT (existing logic, kept intact)
    # ════════════════════════════════════════════════════════

    def draw_edges(self, context, bvh_hit_data, event=None):
        # SHIFT bypass: dynamically suppress ALL circle UI (yellow loop,
        # diameter text, tag line) while SHIFT is held, regardless of
        # click history. The event is the originating MOUSEMOVE event —
        # event.shift is True only while the user holds the SHIFT key,
        # so the bypass tracks the real key state every frame.
        if event is not None and getattr(event, 'shift', False):
            self.tol_drawer_coords.clear()
            self.circle_centre = None
            self.circle_dia = ""
            self._last_circle_dia = ""
            if hasattr(self, 'loop_drawer'):
                self.loop_drawer.disable()
            self.tag_drawer.disable()
            return

        self.tol_drawer_coords.clear()

        # Create loop drawer on-demand like debug_drawer (not pre-created in invoke)
        if not hasattr(self, 'loop_drawer'):
            self.loop_drawer = drawer.ScreenDrawer(
                "loop_drawer", [],
                line_colour=self.colour_loop, line_width=5.0,
                text="", text_colour=self.colour_loop,
                text_size=20, text_pos_x=1, text_pos_y=1)
        closest_edge = None
        dist_last = float('inf')

        # Clamp face_index to valid bmesh range (BVH may have more faces from modifiers)
        bm = bvh_hit_data['bmesh_orig']
        face_idx = min(bvh_hit_data['face_index'], len(bm.faces) - 1)

        # find the closest edge to the cursor
        for edge in bm.faces[face_idx].edges:
            vert_co_1 = edge.verts[0].co
            vert_co_2 = edge.verts[1].co
            closest_point, _x = intersect_point_line(bvh_hit_data['location'], vert_co_1, vert_co_2)
            if (closest_point - vert_co_1).dot(vert_co_2 - vert_co_1) < 0:
                closest_point = vert_co_1
            elif (closest_point - vert_co_2).dot(vert_co_1 - vert_co_2) < 0:
                closest_point = vert_co_2
            dist_curr = (bvh_hit_data['location'] - closest_point).length
            if dist_curr < dist_last:
                closest_edge = edge
                dist_last = dist_curr

        # get a list of connected edges to closest_edge, using the face the mouse is on
        # Clamp face_index to valid bmesh range (BVH may have more faces from modifiers)
        bm = bvh_hit_data['bmesh_orig']
        face_idx = min(bvh_hit_data['face_index'], len(bm.faces) - 1)
        target_face = bm.faces[face_idx]
        edge_loop = self.find_connected_edges(closest_edge, target_face, self.path_tolerance)

        # Create a mapping of source vertices to new vertices in the new bmesh
        bm_edge_loop_copy = bmesh.new()
        vertex_map = {}
        for edge in edge_loop:
            for vertex in edge.verts:
                if vertex not in vertex_map:
                    vertex_map[vertex] = bm_edge_loop_copy.verts.new(vertex.co)
            # Check if this edge already exists to avoid duplicates
            v1 = vertex_map[edge.verts[0]]
            v2 = vertex_map[edge.verts[1]]
            existing = [e for e in v1.link_edges if e.other_vert(v1) == v2]
            if not existing:
                bm_edge_loop_copy.edges.new([v1, v2])

        bm_edge_loop_copy.verts.ensure_lookup_table()
        bm_edge_loop_copy.verts.index_update()
        bm_edge_loop_copy.edges.ensure_lookup_table()
        bm_edge_loop_copy.edges.index_update()

        # remove any verts that are connected to only 1 edge
        while True:
            to_remove = [vert for vert in bm_edge_loop_copy.verts if len(vert.link_edges) < 2]
            if not to_remove:
                break
            for vert in to_remove:
                bm_edge_loop_copy.verts.remove(vert)
            bm_edge_loop_copy.verts.ensure_lookup_table()
            bm_edge_loop_copy.edges.ensure_lookup_table()

        # find and remove any collinear verts
        send_verts = bm_edge_loop_copy.verts[:]
        collinear_verts = self.find_collinear_vertices(send_verts)
        if len(collinear_verts) > 0:
            bmesh.ops.dissolve_verts(bm_edge_loop_copy, verts=collinear_verts)
            bm_edge_loop_copy.verts.ensure_lookup_table()
            bm_edge_loop_copy.edges.ensure_lookup_table()
            bm_edge_loop_copy.faces.ensure_lookup_table()

        if len(bm_edge_loop_copy.edges[:]) > 4:
            loop_closed = True
            for vert in bm_edge_loop_copy.verts:
                if len(vert.link_edges) != 2:
                    loop_closed = False
            if loop_closed:
                circle_dia, circle_centre = self.is_circle(bm_edge_loop_copy)
            else:
                circle_dia = ""
                circle_centre = None

            if circle_centre is not None:
                self.circle_centre = circle_centre.copy()
                self.circle_dia = circle_dia
                dist_last = float('inf')
                edge_point = None
                for edge in bm_edge_loop_copy.edges:
                    vert_co_1 = edge.verts[0].co
                    vert_co_2 = edge.verts[1].co
                    closest_point, _x = intersect_point_line(bvh_hit_data['location'], vert_co_1, vert_co_2)
                    if (closest_point - vert_co_1).dot(vert_co_2 - vert_co_1) < 0:
                        closest_point = vert_co_1
                    elif (closest_point - vert_co_2).dot(vert_co_1 - vert_co_2) < 0:
                        closest_point = vert_co_2
                    dist_curr = (bvh_hit_data['location'] - closest_point).length
                    if dist_curr < dist_last:
                        dist_last = dist_curr
                        edge_point = closest_point.copy() if closest_point else None

                loop_coords = []
                for edge in bm_edge_loop_copy.edges:
                    for vert in edge.verts:
                        loop_coords.append(vert.co.copy())
                self.loop_drawer.update_vertex_coords(loop_coords)
                self.loop_drawer.enable()
                # Force re-registration so the loop draws AFTER the
                # face_fill_drawer (translucent cyan TRIS) — otherwise the
                # cyan fill paints over the yellow circle edges.
                self.loop_drawer.raise_to_front()

                self.tag_drawer_coords.clear()
                self.tag_drawer.update_text(str(circle_dia))
                # Make diameter text follow the circle centre in 3D space so it moves with the viewport
                self.tag_drawer.set_text_world_pos(circle_centre.copy(), offset_x=0, offset_y=25)
                if edge_point:
                    self.tag_drawer_coords.append(edge_point.copy())
                self.tag_drawer_coords.append(circle_centre.copy())
                self.tag_drawer.update_vertex_coords(self.tag_drawer_coords)
                self.tag_drawer.enable()
                context.area.tag_redraw()
            else:
                self.circle_centre = None
                self.circle_dia = ""
                self.tag_drawer.disable()
                self.loop_drawer.disable()
                context.area.tag_redraw()
        else:
            self.circle_centre = None
            self.circle_dia = ""
            self.tag_drawer.disable()
            self.loop_drawer.disable()
            context.area.tag_redraw()

        bm_edge_loop_copy.free()

    def find_connected_edges(self, starting_edge, target_face, tolerance):
        """
        Walk the edge loop in both directions from the starting edge using
        vertex topology and edge alignment. At each vertex, the next edge in
        the loop is the one most aligned with the incoming edge direction
        (highest dot product). This works for:
          - Circular loops on flat faces (cylinder end caps)
          - Edge loops through the middle of objects (holes, recesses)
        Stops when the loop closes back on itself or the next edge is ambiguous.

        The tolerance parameter controls how aggressively the loop is followed:
          - tolerance >= 0.5: standard behaviour (threshold ~0.2)
          - tolerance < 0.5: stricter, stops earlier on ambiguity
          - tolerance > 0.5: more aggressive, follows through tighter corners
        """
        # Map tolerance (range -1 to 1) to ambiguity threshold (range 0.01 to 0.5)
        # Higher tolerance = more aggressive = lower threshold
        ambiguity_threshold = max(0.01, min(0.5, 0.3 - tolerance * 0.3))

        def walk_loop(start_edge, start_vert, visited_set):
            """
            Walk one direction from start_edge, starting at start_vert.
            Returns (edge_list, is_closed) where is_closed=True means
            the walk looped back to the starting edge.
            """
            result = []
            current_edge = start_edge
            current_vert = start_vert

            for _ in range(10000):  # Safety limit
                if current_edge in visited_set and result:
                    # Already visited means we closed a full loop
                    break
                visited_set.add(current_edge)
                result.append(current_edge)

                # Get the far vertex and direction of travel
                next_vert = current_edge.other_vert(current_vert)
                travel_dir = (next_vert.co - current_vert.co).normalized()

                # Find the best connected edge at next_vert to continue the loop
                best_edge = None
                best_dot = -2.0  # Dot product range is -1 to 1
                second_best_dot = -2.0

                for e in next_vert.link_edges:
                    if e == current_edge:
                        continue
                    # Get direction from next_vert through candidate edge
                    cand_other = e.other_vert(next_vert)
                    cand_dir = (cand_other.co - next_vert.co).normalized()
                    dot = travel_dir.dot(cand_dir)
                    if dot > best_dot:
                        second_best_dot = best_dot
                        best_dot = dot
                        best_edge = e
                    elif dot > second_best_dot:
                        second_best_dot = dot

                # If no candidate found, or the best is ambiguous (similar to second best), stop
                if best_edge is None:
                    return result, False
                if best_dot - second_best_dot < ambiguity_threshold and second_best_dot > -1.5:
                    # Ambiguous - multiple edges with similar alignment
                    return result, False

                # Check if we've closed the loop back to the start
                if best_edge == start_edge:
                    result.append(best_edge)
                    visited_set.add(best_edge)
                    return result, True

                current_edge = best_edge
                current_vert = next_vert

            return result, False

        # Walk forward from vert[0]
        forward, closed = walk_loop(starting_edge, starting_edge.verts[0], set())

        if closed:
            # Complete closed loop — use it directly
            return forward

        # If not closed, walk backward from vert[1] and combine
        backward, _ = walk_loop(starting_edge, starting_edge.verts[1], set())

        # Combine: backward (reversed, excluding duplicating start) + forward
        combined = []
        for e in reversed(backward):
            if e not in combined:
                combined.append(e)
        for e in forward:
            if e not in combined:
                combined.append(e)

        if not combined:
            return [starting_edge]
        return combined

    def is_circle(self, edge_loop):
        # check the length of each edge, they all should be the same
        length_check = []
        for edge in edge_loop.edges:
            length_check.append(edge.calc_length())
        if max(length_check) - min(length_check) > self.circle_tolerance:
            return "", None

        # get unique vertices from the selected edge loop
        vertices = set()
        for edge in edge_loop.edges:
            vertices.add(edge.verts[0])
            vertices.add(edge.verts[1])

        # check the angle between edge pairs, they all should be the same
        angle_check = []
        for vert in vertices:
            angle_check.append(vert.calc_edge_angle())
        if max(angle_check) - min(angle_check) > self.circle_tolerance:
            return "", None

        # find the centroid of the vertices
        centroid = Vector((0, 0, 0))
        for vert in vertices:
            centroid += vert.co
        centroid /= len(vertices)

        # get distances from each vertex to the centroid
        distances = []
        for vert in vertices:
            distance = (vert.co - centroid).length
            distances.append(distance)

        # calculate variance to determine circularity
        variance = abs(max(distances) - min(distances))

        if variance > self.circle_tolerance:
            return "", None
        else:
            mid_point = Vector()
            for vert in edge_loop.verts:
                mid_point += vert.co
            mid_point = mid_point / len(edge_loop.edges)
            dists = [(vert.co - mid_point).length for vert in edge_loop.verts]

            # return diameter
            if self.scene_units == 'METRIC' or self.scene_units == 'NONE':
                dia_text = "Ø" + str("%.2f" % ((sum(dists) / len(dists)) * 2) + "mm")
            if self.scene_units == 'IMPERIAL':
                dia_text = "Ø" + str("%.3f" % ((sum(dists) / len(dists)) * 2 / 25.4) + "\"")
            return dia_text, centroid

    def find_collinear_vertices(self, verts, tolerance=0.001):
        """
        Identify vertices shared by collinear edges in the given list.
        """
        collinear_vertices = set()

        for vert in verts:
            if len(vert.link_edges[:]) == 2:
                angle = vert.calc_edge_angle()
                if angle < tolerance:
                    collinear_vertices.add(vert)

        return list(collinear_vertices)


def draw_measure_header(self, context):
    """Draw measure controls in the viewport header during modal operation."""
    layout = self.layout
    props = context.scene.caliper_props
    layout.label(text="Measure")
    layout.prop(props, "measure_mode", text="Mode")
    layout.prop(props, "selection_mode", text="Selection")
    layout.prop(props, "show_xyz", text="Show XYZ", toggle=True)
