"""Write the scene out in every requested format. Runs INSIDE Blender.

Each format is attempted independently. One exporter failing must not lose the
others - a user who can open the .fbx does not care that .stl needed a triangle
pass and did not get one.
"""

from __future__ import annotations

import os
import traceback

import bpy


def _select_all_meshes():
    bpy.ops.object.select_all(action="DESELECT")
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    for obj in meshes:
        obj.select_set(True)
    if meshes:
        bpy.context.view_layer.objects.active = meshes[0]
    return meshes


def export_blend(path: str) -> None:
    """The master file: hierarchy, live modifiers, materials, custom properties."""
    bpy.ops.wm.save_as_mainfile(filepath=path, compress=True, copy=True)


def export_fbx(path: str) -> None:
    """For 3ds Max and Maya. Keeps the object hierarchy and names."""
    _select_all_meshes()
    bpy.ops.export_scene.fbx(
        filepath=path,
        use_selection=False,
        # Hierarchy and custom properties are the point of this export; without
        # them the file is one anonymous blob and the editability promise is
        # broken.
        object_types={"MESH", "EMPTY"},
        use_custom_props=True,
        mesh_smooth_type="FACE",
        apply_scale_options="FBX_SCALE_NONE",
        axis_forward="-Z",
        axis_up="Y",
        bake_space_transform=False,
        use_mesh_modifiers=True,
    )


def export_obj(path: str) -> None:
    """Universal interchange. Groups per object; loses modifiers."""
    bpy.ops.wm.obj_export(
        filepath=path,
        export_selected_objects=False,
        export_materials=True,
        # Both group options off. The exporter writes one `o <object name>` per
        # object regardless, which is the clean per-part split importers use.
        # With them on it *also* emits `g <object>_<mesh>` lines, so a part
        # arrives as `leg_1_leg_1` and the name no longer matches the spec.
        export_material_groups=False,
        export_object_groups=False,
        apply_modifiers=True,
        forward_axis="NEGATIVE_Z",
        up_axis="Y",
    )


def export_glb(path: str) -> None:
    """For the in-browser viewer."""
    bpy.ops.export_scene.gltf(
        filepath=path,
        export_format="GLB",
        use_selection=False,
        export_apply=True,
        export_extras=True,       # carries the forge_* custom properties
        export_yup=True,
    )


def export_stl(path: str) -> None:
    """For 3D printing. Geometry only - no materials, no hierarchy."""
    bpy.ops.wm.stl_export(
        filepath=path,
        export_selected_objects=False,
        apply_modifiers=True,
        ascii_format=False,
    )


EXPORTERS = {
    "blend": (export_blend, "blend"),
    "fbx": (export_fbx, "fbx"),
    "obj": (export_obj, "obj"),
    "glb": (export_glb, "glb"),
    "stl": (export_stl, "stl"),
}


def export_all(out_dir: str, name: str, formats: list[str]) -> list[dict]:
    results = []
    for fmt in formats:
        entry = EXPORTERS.get(fmt)
        if entry is None:
            results.append({"format": fmt, "ok": False, "error": "unsupported format"})
            continue

        function, extension = entry
        path = os.path.join(out_dir, f"{name}.{extension}")
        try:
            function(path)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            if size == 0:
                results.append({"format": fmt, "ok": False,
                                "error": "the exporter wrote nothing"})
                continue
            results.append({"format": fmt, "ok": True,
                            "file": f"{name}.{extension}", "bytes": size})
        except Exception as exc:  # noqa: BLE001
            # Keep going. A format that fails is one download missing, not a
            # failed build.
            results.append({
                "format": fmt, "ok": False,
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "trace": traceback.format_exc()[-600:],
            })
    return results
