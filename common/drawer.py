import gpu
import blf
from bpy_extras import view3d_utils
from gpu.types import GPUVertBuf, GPUVertFormat
from gpu_extras.batch import batch_for_shader
from . import handlers


class ScreenDrawer:
    def __init__(self, name, vertex_coords, line_colour=(1.0, 0.5, 0.0, 1.0), line_width=20.0, text="", text_colour=(1.0, 0.5, 0.0, 1.0), text_size=20, text_pos_x=1, text_pos_y=1, draw_type='LINES'):
        """
        Initialize the LineDrawer with vertex coordinates and line colour.
        :param name: string - Name of the ScreenDrawer
        :param vertex_coords: list - Vector x, y, z representing vertex coordinates
        :param line_colour: tuple - r, g, b, a for the line colour
        :param line_width: float - Width of the lines being drawn
        :param text: string - Text to be drawn
        :param text_colour: tuple - r, g, b, a for the text colour
        :param line_width: float - Width of the lines being drawn
        :param text_size: int - Size of the text to be drawn
        :param text_pos_x: int - X location in pixels of the text to be drawn
        :param text_pos_y: int - Y location in pixels of the text to be drawn
        :param draw_type: str - 'LINES' for lines, 'TRIS' for filled triangles
        """
        self.name = name
        self.vertex_coords = vertex_coords
        self.line_colour = line_colour
        self.line_width = line_width
        self.text = text
        self.text_colour = text_colour
        self.text_size = text_size
        self.text_pos_x = text_pos_x
        self.text_pos_y = text_pos_y
        self.text_world_pos = None  # If set, text follows this 3D world position
        self.text_world_offset_x = 0
        self.text_world_offset_y = 0
        self.draw_type = draw_type  # 'LINES' or 'TRIS'
        self.shader = self._create_shader()
        self.vertex_buffer = self._create_vertex_buffer(self.vertex_coords)
        self.handle_lines = None
        self.handle_text = None
        # Batch caching: batch_for_shader uploads the entire coord list to the
        # GPU every time it's called. For TRIS drawers with thousands of vertices
        # (e.g. face fill on a complex mesh), this is catastrophic if called every
        # frame. We cache the batch and only recreate it when coords change.
        self._batch = None
        self._coords_dirty = True
        # Screen-space overlay mode: when True, vertex_coords is ignored
        # and the drawer renders only screen-space text (multi-line,
        # anchored to the lower-left corner of the viewport).
        self.screen_overlay = False
        self.screen_overlay_lines = []  # list of (text, colour) tuples
        self.screen_overlay_anchor = 'BOTTOM_LEFT'  # or 'TOP_LEFT', etc.
        self.screen_overlay_margin = 16  # pixels from the viewport edge
        # Set of line indices that should render a small icon (ruler
        # glyph approximating Blender's DRIVER_DISTANCE) immediately
        # before the line's text. Populated by set_screen_overlay_lines
        # when the caller passes a line tagged with 'icon': True.
        self.screen_overlay_icon_lines = set()

    def _create_shader(self):
        """
        Create and return a GPUShader for drawing lines.
        """
        shader_info = gpu.types.GPUShaderCreateInfo()

        # Define vertex input
        shader_info.vertex_in(0, 'VEC3', "position")

        # Define uniforms
        shader_info.push_constant('MAT4', "u_ViewProjectionMatrix")
        shader_info.push_constant('VEC4', "u_Linecolour")

        # Define fragment output
        shader_info.fragment_out(0, 'VEC4', "FragColor")

        # Set shader sources
        shader_info.vertex_source('''
        void main() {
            gl_Position = u_ViewProjectionMatrix * vec4(position, 1.0);
        }
        ''')

        shader_info.fragment_source('''
        void main() {
            FragColor = u_Linecolour;
        }
        ''')

        return gpu.shader.create_from_info(shader_info)

    def _create_vertex_buffer(self, vertex_coords):
        """
        Create a GPUVertBuf for the vertex coordinates.
        """
        format = GPUVertFormat()
        pos_attr = format.attr_add(id="position", comp_type='F32', len=3, fetch_mode='FLOAT')

        vertex_buffer = GPUVertBuf(format=format, len=len(vertex_coords))
        vertex_buffer.attr_fill(id=pos_attr, data=vertex_coords)

        return vertex_buffer

    def _draw_lines_callback(self, context):
        """
        Internal draw callback function for rendering the lines.
        Supports both 'LINES' and 'TRIS' draw types.

        Performance: the GPU batch is cached and only rebuilt when the
        vertex coords actually change (via update_vertex_coords). This
        avoids re-uploading thousands of vertices to the GPU every frame,
        which is the dominant cost for TRIS drawers on complex meshes.

        Screen-space overlay drawers (screen_overlay=True) skip the
        3D line render and instead draw any requested overlay icons via
        gpu (in this POST_VIEW pass — the text is drawn by the text
        callback in POST_PIXEL).
        """
        if self.screen_overlay:
            # Draw the overlay icons in POST_VIEW so the gpu matrix is
            # valid. The backing panel and text are drawn in POST_PIXEL
            # via the text callback.
            if self.screen_overlay_icon_lines:
                self._draw_overlay_icons(context)
            return
        if not self.shader:
            return

        # Get the region data from the context passed to the callback
        region_data = context.region_data
        if region_data is None:
            return

        # Set the correct uniform names as defined in the shader
        self.shader.uniform_float("u_ViewProjectionMatrix", region_data.perspective_matrix)

        self.shader.uniform_float("u_Linecolour", self.line_colour)

        # Recreate batch only when coords have changed. The previous
        # implementation rebuilt it every frame, which for face-fill on a
        # complex mesh (thousands of triangle verts) meant a full GPU
        # upload 60+ times per second even when nothing changed.
        if self._batch is None or self._coords_dirty:
            prim_type = 'TRIS' if self.draw_type == 'TRIS' else 'LINES'
            self._batch = batch_for_shader(
                self.shader, prim_type, {"position": self.vertex_coords})
            self._coords_dirty = False

        gpu.state.blend_set('ALPHA')
        if self.draw_type != 'TRIS':
            gpu.state.line_width_set(self.line_width)

        gpu.state.depth_test_set('ALWAYS')  # Ensure fill is visible through geometry

        self._batch.draw(self.shader)

    def _draw_text_callback(self, context):
        """
        Internal draw callback function for rendering text.
        If text_world_pos is set, the text position is projected from 3D space each frame
        so it follows the location. The text is centered on the target position.

        Screen-space overlay mode: when screen_overlay=True, screen_overlay_lines
        is rendered as multiple lines anchored to a viewport corner. Used by
        the XYZ data widget in measure.py — each row is (text, colour).
        """

        # Screen-space multi-line overlay (e.g. XYZ data widget)
        if self.screen_overlay:
            self._draw_screen_overlay(context)
            return

        font_id = 0
        blf.size(font_id, self.text_size)

        # If a world position is set, compute screen position each frame
        if self.text_world_pos is not None and context.region and context.region_data:
            screen_pos = view3d_utils.location_3d_to_region_2d(
                context.region, context.region_data, self.text_world_pos)
            if screen_pos:
                self.text_pos_x = int(screen_pos.x + self.text_world_offset_x)
                self.text_pos_y = int(screen_pos.y + self.text_world_offset_y)

        # Measure text dimensions to center it on the position
        text_width, text_height = blf.dimensions(font_id, self.text)
        centered_x = self.text_pos_x - int(text_width / 2)
        centered_y = self.text_pos_y - int(text_height / 2)

        blf.color(font_id, self.text_colour[0], self.text_colour[1], self.text_colour[2], self.text_colour[3])
        blf.enable(0, blf.SHADOW)
        blf.shadow(0, 3, 0, 0, 0, 1)
        blf.shadow_offset(0, 1, 1)
        blf.position(font_id, centered_x, centered_y, 0)
        blf.draw(font_id, self.text)

    def _draw_screen_overlay(self, context):
        """
        Render screen_overlay_lines anchored to the lower-left corner of the
        viewport. Each row is (text, colour). Lines are stacked vertically
        and read top-down. Icons (ruler glyphs) are drawn by
        _draw_overlay_icons in the POST_VIEW lines callback.
        """
        if not self.screen_overlay_lines:
            return

        font_id = 0
        blf.size(font_id, self.text_size)

        # Find the viewport region (the actual 3D area, not panels/header)
        region = None
        area = getattr(context, 'area', None)
        if area is not None:
            for r in area.regions:
                if r.type == 'WINDOW':
                    region = r
                    break
        if region is None:
            region = getattr(context, 'region', None)
        if region is None:
            return

        margin = self.screen_overlay_margin
        # Vertical position: stack from the bottom upward
        line_height = self.text_size + 4
        base_y = margin
        x = margin

        # Optional semi-transparent background panel for legibility —
        # we draw a filled rectangle behind the text only when there is
        # more than one line, so single-line overlays stay minimal.
        if len(self.screen_overlay_lines) > 1:
            # Compute total width/height to draw the panel.
            max_w = 0
            for line_text, _ in self.screen_overlay_lines:
                w, _ = blf.dimensions(font_id, line_text)
                if w > max_w:
                    max_w = w
            total_h = line_height * len(self.screen_overlay_lines) + 8
            try:
                gpu.state.blend_set('ALPHA')
                shader = self.shader
                # Build a 4-vertex quad in screen space
                verts = [
                    (x - 8, base_y - 8, 0.0),
                    (x + max_w + 8, base_y - 8, 0.0),
                    (x + max_w + 8, base_y + total_h - 8, 0.0),
                    (x - 8, base_y + total_h - 8, 0.0),
                ]
                fmt = gpu.types.GPUVertFormat()
                pos_attr = fmt.attr_add(id="position", comp_type='F32', len=3, fetch_mode='FLOAT')
                vbuf = gpu.types.GPUVertBuf(fmt, 4)
                vbuf.attr_fill(id=pos_attr, data=verts)
                # Use 2D projection — ortho-style. Build a 4x4 mat4 that maps
                # pixel coords (x, y) to NDC directly.
                from mathutils import Matrix
                region_w = region.width
                region_h = region.height
                # Pixel (0,0) is bottom-left in blf, but we draw quads in NDC.
                # Convert: NDC.x = px / (w/2) - 1, NDC.y = py / (h/2) - 1
                proj = Matrix((
                    (2.0 / region_w, 0, 0, -1),
                    (0, 2.0 / region_h, 0, -1),
                    (0, 0, 1, 0),
                    (0, 0, 0, 1),
                ))
                shader.uniform_float("u_ViewProjectionMatrix", proj)
                shader.uniform_float("u_Linecolour", (0.85, 0.85, 0.85, 0.85))
                batch = batch_for_shader(shader, 'TRIS', {"position": verts})
                # Triangle fan from verts (0,1,2,0,2,3)
                fan_verts = [verts[0], verts[1], verts[2],
                             verts[0], verts[2], verts[3]]
                fan_buf = gpu.types.GPUVertBuf(fmt, len(fan_verts))
                fan_buf.attr_fill(id=pos_attr, data=fan_verts)
                fan_batch = batch_for_shader(shader, 'TRIS', {"position": fan_verts})
                gpu.state.depth_test_set('NONE')
                fan_batch.draw(shader)
            except Exception:
                pass

        blf.enable(0, blf.SHADOW)
        blf.shadow(0, 3, 0, 0, 0, 1)
        blf.shadow_offset(0, 1, 1)

        # Draw each line from bottom upward
        for i, (line_text, line_colour) in enumerate(self.screen_overlay_lines):
            y = base_y + i * line_height
            if line_text:
                blf.color(font_id, line_colour[0], line_colour[1], line_colour[2], line_colour[3])
                blf.position(font_id, x, y, 0)
                blf.draw(font_id, line_text)
        # Icons are drawn by _draw_overlay_icons in the POST_VIEW pass
        # (called from _draw_lines_callback). Doing the gpu draw here
        # in POST_PIXEL would fail — there's no valid view-projection
        # matrix in the text-pass context.

    def enable(self):
        """
        Enable the draw callback.
        """
        if self.handle_lines is None:
            self.handle_lines = handlers.DrawHandlerManager.add_handler(self.name + "_lines", "LINES", self._draw_lines_callback)

        if self.handle_text is None:
            self.handle_text = handlers.DrawHandlerManager.add_handler(self.name + "_text", "TEXT", self._draw_text_callback)

    def disable(self):
        """
        Disable the draw callback.
        """
        if self.handle_lines is not None:
            handlers.DrawHandlerManager.remove_handler(self.name + "_lines")
            self.handle_lines = None

        if self.handle_text is not None:
            handlers.DrawHandlerManager.remove_handler(self.name + "_text")
            self.handle_text = None

    def raise_to_front(self):
        """
        Force this drawer's handlers to draw LAST (on top of everything else
        registered earlier). Blender draws POST_VIEW/POST_PIXEL handlers in
        registration order, so removing + re-adding bumps the drawer to the
        end of the queue. Used by the circle loop drawer in measure.py so
        the yellow circle always paints over the translucent cyan face fill.
        """
        if self.handle_lines is not None:
            handlers.DrawHandlerManager.remove_handler(self.name + "_lines")
            self.handle_lines = handlers.DrawHandlerManager.add_handler(
                self.name + "_lines", "LINES", self._draw_lines_callback)
        if self.handle_text is not None:
            handlers.DrawHandlerManager.remove_handler(self.name + "_text")
            self.handle_text = handlers.DrawHandlerManager.add_handler(
                self.name + "_text", "TEXT", self._draw_text_callback)

        # Screen-overlay icon handler (only used when screen_overlay=True
        # and screen_overlay_icon_lines has entries). Registered as a
        # LINES handler so it runs in POST_VIEW with a valid GPU matrix
        # context. We re-use the same callback as the 3D-line pass — it
        # short-circuits when screen_overlay=True and the overlay
        # callback draws the icon shapes via gpu at that point.
        icon_handler_name = self.name + "_overlay_icons"
        if getattr(self, '_handle_overlay_icons', None) is not None:
            handlers.DrawHandlerManager.remove_handler(icon_handler_name)
            self._handle_overlay_icons = None

    def update_vertex_coords(self, vertex_coords):
        """
        Update the vertex coordinates via the vertex buffer
        :param vertex_coords: A list of (x, y, z) tuples in object space.
        """
        self.vertex_coords = vertex_coords
        # Mark the cached batch as stale so the draw callback rebuilds it
        # on the next frame. Without this, batch_for_shader would upload
        # stale vertices to the GPU.
        self._coords_dirty = True

    def update_line_colour(self, line_colour):
        """
        Update the line colour.
        :param line_colour: A tuple (r, g, b, a) for the new line colour.
        """
        self.line_colour = line_colour

    def update_line_width(self, line_width):
        """
        Update the line width.
        :param line_width: float
        """
        self.line_width = line_width

    def update_text(self, text):
        """
        Update the text
        """
        self.text = text

    def update_text_colour(self, text_colour):
        """
        Update the text colour
        """
        self.text_colour = text_colour

    def update_text_pos(self, text_pos_x, text_pos_y):
        """
        Update the text position
        """
        self.text_pos_x = text_pos_x
        self.text_pos_y = text_pos_y

    def set_text_world_pos(self, world_pos, offset_x=0, offset_y=0):
        """
        Set a 3D world position that the text should follow.
        The text will be projected to screen space each frame.
        :param world_pos: Vector - 3D world position
        :param offset_x: int - pixel offset from projected screen position
        :param offset_y: int - pixel offset from projected screen position
        """
        self.text_world_pos = world_pos
        self.text_world_offset_x = offset_x
        self.text_world_offset_y = offset_y

    def clear_text_world_pos(self):
        """Stop following a 3D world position."""
        self.text_world_pos = None

    def _draw_overlay_icons(self, context):
        """
        Approximate Blender's built-in icon glyphs (DRIVER_DISTANCE and
        any others the caller tagged in set_screen_overlay_lines) by
        rendering a small GPU-drawn shape immediately to the left of
        each tagged line. blf has no icon API and UILayout doesn't
        redraw on modal frames, so we draw a "ruler" shape (horizontal
        line with two tick marks) which visually matches the DRIVER_
        DISTANCE icon used elsewhere in the addon.

        Lines tagged via the 3-tuple form of set_screen_overlay_lines
        (text, colour, icon_name) get a ruler glyph at the line's
        vertical position. icon_name is currently treated as "draw a
        ruler" regardless of the exact identifier, since the user has
        only requested DRIVER_DISTANCE.
        """
        if not self.screen_overlay_icon_lines:
            return
        # Locate the viewport region
        region = None
        area = getattr(context, 'area', None)
        if area is not None:
            for r in area.regions:
                if r.type == 'WINDOW':
                    region = r
                    break
        if region is None:
            region = getattr(context, 'region', None)
        if region is None:
            return

        try:
            from mathutils import Matrix
        except Exception:
            return

        margin = self.screen_overlay_margin
        line_height = self.text_size + 4
        region_w = region.width
        region_h = region.height
        proj = Matrix((
            (2.0 / region_w, 0, 0, -1),
            (0, 2.0 / region_h, 0, -1),
            (0, 0, 1, 0),
            (0, 0, 0, 1),
        ))

        # Local-space ruler geometry: a horizontal rule with two tick
        # marks (left edge, centre tick, right edge). 18 px wide × 16 px
        # tall, drawn as thin triangles so it renders reliably without
        # depending on line_width support in the user's GPU/driver.
        icon_w = 18
        icon_h = 16
        # Vertices in local icon space (0,0 = bottom-left of the icon)
        local_verts = [
            (0.0, 0.0),                # 0: bottom-left
            (icon_w, 0.0),             # 1: bottom-right
            (icon_w, icon_h),          # 2: top-right
            (0.0, icon_h),             # 3: top-left
            (icon_w * 0.5, icon_h * 0.4),  # 4: centre-tick bottom
            (icon_w * 0.5, icon_h * 0.7),  # 5: centre-tick top
        ]
        # Line segments expressed as (start, end) vertex-index pairs
        line_pairs = [
            (0, 1),   # bottom horizontal
            (0, 3),   # left vertical
            (1, 2),   # right vertical
            (4, 5),   # centre tick
            (2, 3),   # top horizontal
        ]
        fmt = gpu.types.GPUVertFormat()
        pos_attr = fmt.attr_add(id="position", comp_type='F32', len=3, fetch_mode='FLOAT')
        shader = self.shader

        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        gpu.state.line_width_set(2.0)
        shader.uniform_float("u_ViewProjectionMatrix", proj)

        for i in self.screen_overlay_icon_lines:
            if i >= len(self.screen_overlay_lines):
                continue
            line_colour = self.screen_overlay_lines[i][1]
            # Position the icon at the start of the tagged line.
            ox = margin
            oy = margin + i * line_height
            triangles = []
            for a_idx, b_idx in line_pairs:
                ax, ay = local_verts[a_idx]
                bx, by = local_verts[b_idx]
                ax += ox
                bx += ox
                ay += oy
                by += oy
                dx = bx - ax
                dy = by - ay
                length = (dx * dx + dy * dy) ** 0.5
                if length == 0:
                    continue
                # Perpendicular (1 px wide) for the line quad
                px = -dy / length
                py = dx / length
                hw = 0.6
                p1 = (ax + px * hw, ay + py * hw, 0.0)
                p2 = (ax - px * hw, ay - py * hw, 0.0)
                p3 = (bx - px * hw, by - py * hw, 0.0)
                p4 = (bx + px * hw, by + py * hw, 0.0)
                triangles.extend([p1, p2, p3, p1, p3, p4])
            if not triangles:
                continue
            buf = gpu.types.GPUVertBuf(fmt, len(triangles))
            buf.attr_fill(id=pos_attr, data=triangles)
            batch = batch_for_shader(shader, 'TRIS', {"position": triangles})
            shader.uniform_float("u_Linecolour",
                                  (line_colour[0], line_colour[1],
                                   line_colour[2], line_colour[3]))
            batch.draw(shader)

    def set_screen_overlay_lines(self, lines):
        """
        Set the screen-space overlay text lines for this drawer.
        :param lines: list of tuples. Either (text, colour) or
            (text, colour, icon_name). Pass icon_name='DRIVER_DISTANCE'
            (or any other Blender icon identifier — see
            bpy.types.UILayout.bl_rna.properties for the full list) to
            render a small ruler glyph before the line. We approximate
            the icon shape via gpu since blf has no icon API and panel
            redraws aren't available from a modal handler.
        """
        self.screen_overlay = True
        self.screen_overlay_lines = []
        self.screen_overlay_icon_lines = set()
        for i, entry in enumerate(lines):
            if len(entry) == 3:
                text, colour, icon_name = entry
                self.screen_overlay_lines.append((text, colour))
                if icon_name:
                    self.screen_overlay_icon_lines.add(i)
            else:
                text, colour = entry
                self.screen_overlay_lines.append((text, colour))

    def clear_screen_overlay(self):
        """Disable the screen-space overlay mode."""
        self.screen_overlay = False
        self.screen_overlay_lines = []