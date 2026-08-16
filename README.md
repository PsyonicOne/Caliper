# Caliper — Measure Tool

A CAD-style distance and angle measurement tool for Blender 4.0+. Auto-detects circular geometry (holes, bosses, fillets) and snaps measurements to circle centres and diameters.

## Install

1. Download/zip the `Caliper/` folder.
2. In Blender: *Edit → Preferences → Add-ons → Install…*
3. Select the zip (or the folder) and enable **3D View: Caliper — Measure Tool**.
4. Open the 3D View sidebar (N) and find the **Caliper** tab.

## Usage

1. Press **Alt+M** in the 3D Viewport (or click the **Measure** button in the Caliper sidebar panel).
2. Hover over a face/edge — if the cursor is over a circle, the diameter is displayed and the snap point becomes the circle centre.
3. Click once for the first endpoint, click again for the second. The total distance appears in world space at the midpoint and in the lower-left data widget.
4. Press **RMB** or **Esc** to exit.

The launch hotkey is registered on the 3D View keyconfig and can be rebound via *Edit → Preferences → Keymap → 3D View → Measure (Caliper)*. The current binding and all in-modal hotkeys are also displayed in the addon's own Preferences panel (*Edit → Preferences → Add-ons → Caliper — Measure Tool*).

## Hotkeys (active during modal)

| Key | Action |
|-----|--------|
| **LMB** | Select endpoint |
| **SHIFT + LMB** | Bypass circle detection (snap to face/edge centroid) |
| **RMB / Esc** | Cancel / exit |
| **D** | Switch to Distance mode |
| **A** | Switch to Angle mode |
| **1** | Selection mode: Face |
| **2** | Selection mode: Edge |
| **3** | Selection mode: Vertex (disables circle detection) |
| **Ctrl + Wheel** | Adjust path tolerance (loop-follow aggressiveness) |
| **Alt + Wheel** | Adjust circle tolerance (max edge-length / radius variance) |
| **Shift + Wheel** | Fine-grained tolerance adjustment (combined with Ctrl or Alt) |

## Scene properties

Stored on `bpy.types.Scene.CADView_props`:

- `path_tol` — controls how aggressively the edge loop is followed during circle detection (`-1.0` strict → `+1.0` aggressive)
- `circle_tol` — max allowed variance in candidate-circle edges / angles / radii
- `measure_mode` — `distance` or `angle`
- `selection_mode` — `face`, `edge`, or `vertex`
- `show_xyz` — toggle the 3D X/Y/Z axis component lines (the lower-left data widget stays visible regardless)

## Files

```
Caliper/
├── __init__.py       bl_info + scene/AddonPreferences + keymap + register/unregister
├── panel.py          Sidebar panel (Caliper tab)
├── measure.py        CAD_VIEW_OT_measure modal operator
└── common/
    ├── __init__.py
    ├── drawer.py     ScreenDrawer (GPU lines + TRIS + screen/world text)
    ├── bvh_ray.py    Cached per-object BVH ray casting
    └── handlers.py   DrawHandlerManager singleton
```