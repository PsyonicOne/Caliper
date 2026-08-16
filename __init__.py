import bpy
from bpy.props import (
    EnumProperty,
    FloatProperty,
    PointerProperty,
    BoolProperty,
    StringProperty,
)
from bpy.types import PropertyGroup, AddonPreferences

bl_info = {
    "name": "Caliper",
    "author": "Ash",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Caliper",
    "description": "Modern CAD-style distance and angle measurement tool with auto-detected circle snapping, X/Y/Z axis deltas, and screen-space readouts.",
    "category": "3D View",
}


# ── Scene property group ──

_MEASURE_MODE_ITEMS = (
    ("distance", "Distance", "Measure distance between two points (auto-detects circles)"),
    ("angle", "Angle", "Measure angle between two elements"),
)

_SELECTION_MODE_ITEMS = (
    ("face", "Face", "Snap to faces / face centroids"),
    ("edge", "Edge", "Snap to edge midpoints"),
    ("vertex", "Vertex", "Snap to vertices (disables circle detection)"),
)


class CaliperSceneProperties(PropertyGroup):
    path_tol: FloatProperty(
            name="Path Tolerance",
            description="Tolerance for following edge loops during circle detection",
            default=0.0,
            soft_min=-1.0,
            soft_max=1.0,
            step=10,
            precision=2,
        )
    circle_tol: FloatProperty(
        name="Circle Tolerance",
        description="Maximum allowed variance in edge length / vertex angle / radius for a candidate circle",
        default=0.5,
        soft_min=0.0,
        soft_max=50.0,
        step=1,
        precision=2,
    )
    measure_mode: EnumProperty(
        name="Mode",
        items=_MEASURE_MODE_ITEMS,
        default="distance",
    )
    selection_mode: EnumProperty(
        name="Selection",
        items=_SELECTION_MODE_ITEMS,
        default="face",
    )
    show_xyz: BoolProperty(
        name="Show XYZ",
        description="Show the X/Y/Z axis component lines in the 3D viewport. The lower-left data widget is always visible.",
        default=True,
    )


# ── Addon preferences ──

class CaliperAddonPreferences(AddonPreferences):
    bl_idname = __name__  # "Caliper" / "Caliper.<something>" — matched by Blender

    # Display-only: the user can't edit this directly, but we read
    # it back from the registered keymap to show the current binding.
    launch_key: StringProperty(
        name="Launch Hotkey",
        description="Current key that launches the Caliper operator (edit via Preferences > Keymap > 3D View > 'Caliper'",
        default="Alt+M",
    )

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="Launch Hotkey", icon="KEYINGSET")
        row = box.row(align=True)
        row.prop(self, "launch_key", text="Key")
        row.label(text="(rebind in Preferences > Keymap > 3D View)")

        box = layout.box()
        box.label(text="In-Modal Hotkeys", icon="EVENT_M")
        col = box.column(align=True)
        col.label(text="LMB — Select endpoint")
        col.label(text="RMB / Esc — Cancel / exit")
        col.label(text="SHIFT + LMB — Bypass circle detection")
        col.label(text="D / A — Distance / Angle mode")
        col.label(text="1 / 2 / 3 — Face / Edge / Vertex selection")
        col.label(text="Ctrl + Wheel — Path tolerance")
        col.label(text="Alt + Wheel — Circle tolerance")
        col.label(text="Shift + Wheel — Fine-grained tolerance (with Ctrl/Alt)")


# Operator + scene property access — assigned at register() time so the
# caliper module can be imported (and statically checked) without
# triggering Blender's registration machinery.
_CALIPER_CLASSES = ()  # populated in register()

# Keymap storage — populated in register(). Kept at module level so
# unregister() can find and remove the keymap items.
_ADDON_KEYMAPS = []


def _register_keymap():
    """Install the default 'M' keymap for the Caliper operator.

    Bound to the 3D View keyconfig. Returns nothing — the keymap is
    remembered in `_ADDON_KEYMAPS` so unregister can find it.
    """
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc is None:
        # Headless / background contexts don't have an addon keyconfig.
        return
    km = kc.keymaps.new(name="3D View", space_type="VIEW_3D")
    kmi = km.keymap_items.new("caliper.measure", "M", "PRESS", alt=True)
    # Override the Keymap UI label — by default Blender shows the
    # operator's bl_label ("Measure"), which is misleading when several
    # Measure-style operators share the same 3D View keymap slot.
    # KeyMapItem.name, when set, takes precedence over the operator's
    # bl_label in the Preferences > Keymap UI.
    kmi.name = "Caliper"
    _ADDON_KEYMAPS.append((km, kmi))


def _unregister_keymap():
    """Remove every keymap item registered by this addon."""
    for km, kmi in _ADDON_KEYMAPS:
        try:
            km.keymap_items.remove(kmi)
        except (ReferenceError, RuntimeError, AttributeError):
            pass
    _ADDON_KEYMAPS.clear()


def register():
    global _CALIPER_CLASSES

    # Property group first — the operator's modal handler reads
    # `context.scene.caliper_props` on every event, so the PointerProperty
    # must exist before the operator is registered.
    bpy.utils.register_class(CaliperSceneProperties)
    bpy.types.Scene.caliper_props = PointerProperty(type=CaliperSceneProperties)

    # Addon preferences next — pure UI, no runtime dependencies.
    bpy.utils.register_class(CaliperAddonPreferences)

    # Operator last — its invoke() reads from `context.scene.caliper_props`.
    from . import caliper as _caliper
    bpy.utils.register_class(_caliper.CALIPER_OT_measure)

    # Sidebar panel
    from . import panel as _panel
    bpy.utils.register_class(_panel.CALIPER_PT_main)

    # Default keymap
    _register_keymap()

    _CALIPER_CLASSES = (
        CaliperSceneProperties,
        CaliperAddonPreferences,
        _caliper.CALIPER_OT_measure,
        _panel.CALIPER_PT_main,
    )


def unregister():
    global _CALIPER_CLASSES

    # Drop keymaps first so any in-flight modal can no longer be
    # re-triggered by the binding while we tear down classes.
    _unregister_keymap()

    # Unregister in reverse order.
    for cls in reversed(_CALIPER_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError):
            pass

    # Drop the Scene pointer so a re-register starts clean.
    if hasattr(bpy.types.Scene, "caliper_props"):
        try:
            del bpy.types.Scene.caliper_props
        except (AttributeError, RuntimeError):
            pass

    _CALIPER_CLASSES = ()


# Convenience entry point — running `blender --python caliper/__init__.py`
# won't auto-register, but having `register()` available lets users
# script it from the text editor:
#     import caliper
#     caliper.register()
if __name__ == "__main__":
    register()
