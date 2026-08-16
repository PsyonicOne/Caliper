"""
Sidebar panel for the Caliper measure tool.

The panel piggy-backs on the existing 'Tool' tab in the 3D View
sidebar so it sits alongside Active Tool / Options / Workspace rather
than spawning its own tab. It exposes:
  - a launch button that invokes `bpy.ops.caliper.measure()`
  - the three persistent scene-property controls (Mode, Selection,
    Show XYZ) so the user can switch them without opening the
    viewport-header dropdowns while the tool is active.

The panel only shows when a 3D View area is the active context, so
it doesn't appear as an empty section elsewhere.
"""

from bpy.types import Panel


class CALIPER_PT_main(Panel):
    bl_idname = "CALIPER_PT_main"
    bl_label = "Caliper"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    # Place inside the user's existing "Tool" sidebar tab rather than
    # spawning a new one. The tab is created by whichever addon
    # registered it first; we just claim a slot inside it.
    bl_category = "Tool"
    # Pin Caliper just below Workspace so it doesn't get pushed off
    # the top by later-added panels. Lower numbers = higher in the
    # panel list (Blender orders by -bl_order within a category).
    bl_order = 10

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == "VIEW_3D"

    def draw(self, context):
        layout = self.layout
        props = context.scene.caliper_props

        col = layout.column(align=True)
        col.scale_y = 1.4
        col.operator("caliper.measure", text="Caliper", icon="DRIVER_DISTANCE")

        layout.separator()

        col = layout.column(align=True)
        col.prop(props, "measure_mode", text="Mode")
        col.prop(props, "selection_mode", text="Selection")
        col.prop(props, "show_xyz", text="Show XYZ")
