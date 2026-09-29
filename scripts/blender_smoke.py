"""Prove this Blender can do the only things the app asks of it.

Run inside the image at build time, so a Blender that cannot export lands as a
failed build rather than as a 500 on somebody's first model:

    blender --background --factory-startup --python scripts/blender_smoke.py -- /tmp/out

Checking `hasattr(bpy.ops.export_scene, "fbx")` does not work: bpy.ops
fabricates attributes on demand, so that test passes for an operator that does
not exist. Checking bpy.types does not work either, because only the Python
addon exporters (FBX, glTF) register a class there while OBJ and STL are
built-in C operators and never appear.

So this exports a cube to all five formats and renders one frame, and calls it
working only if every file exists and is non-empty. It is slower than a
capability check and it is the only version that can actually fail.
"""
import sys, os
import bpy

out = sys.argv[sys.argv.index("--") + 1]

# Printed first, so a build log identifies which version of this script ran.
# A log without this line is from before the output directory was created
# here, which is the difference between a stale log and a live failure.
print(f"SMOKE_VERSION:3 out={out} exists={os.path.isdir(out)}", flush=True)


def die(message):
    """Report and exit non-zero.

    Blender exits 0 when a script raises, so an uncaught exception here would
    pass the Docker build while proving nothing. Every path out of this script
    has to end in an explicit exit code.
    """
    print("SMOKE_FAILURES:" + message)
    sys.exit(1)


# Created here, exactly as forge/build/build.py does for a real build. Without
# it every exporter fails with "No such file or directory" naming the *file*,
# which reads like a broken exporter rather than a missing directory - and the
# render still succeeds, because Blender's render path creates directories
# while the exporters do not. That combination sent the first Railway build
# looking in the wrong place entirely.
try:
    os.makedirs(out, exist_ok=True)
except OSError as exc:
    die(f"cannot create the output directory {out}: {exc}")

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=1)
bpy.context.active_object.name = "smoke_part"

failures = []
def attempt(label, fn):
    try:
        fn()
    except Exception as exc:
        failures.append(f"{label}: {type(exc).__name__}: {exc}")

attempt("blend", lambda: bpy.ops.wm.save_as_mainfile(filepath=f"{out}/s.blend", compress=True))
attempt("fbx",   lambda: bpy.ops.export_scene.fbx(filepath=f"{out}/s.fbx"))
attempt("obj",   lambda: bpy.ops.wm.obj_export(filepath=f"{out}/s.obj"))
attempt("glb",   lambda: bpy.ops.export_scene.gltf(filepath=f"{out}/s.glb", export_format="GLB"))
attempt("stl",   lambda: bpy.ops.wm.stl_export(filepath=f"{out}/s.stl"))

# A render too: the item page shows four Workbench previews.
scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"
scene.render.resolution_x = scene.render.resolution_y = 64
scene.render.filepath = f"{out}/s.png"
bpy.ops.object.camera_add(location=(3, -3, 2))
scene.camera = bpy.context.active_object
attempt("render", lambda: bpy.ops.render.render(write_still=True))

empty = [n for n in ("s.blend", "s.fbx", "s.obj", "s.glb", "s.stl", "s.png")
         if not os.path.exists(f"{out}/{n}") or os.path.getsize(f"{out}/{n}") == 0]
if empty:
    failures.append(f"produced nothing for: {empty}")

# --------------------------------------------------------------------------- #
# Every part must survive the round trip as its own object
# --------------------------------------------------------------------------- #
#
# This is the product's central promise: a designer opens the file, clicks one
# leg, types a new width. A file that arrives as a single welded mesh is worth
# almost nothing however accurate its dimensions are, and the failure is
# invisible from this side of the export - the scene is correct, the file is
# non-empty, and the parts only turn out to be fused when somebody opens it.
#
# So the exports are read back in and counted. Three objects go out; three
# objects, still individually named, must come back.
#
# STL is exempt and always will be: the format has no concept of an object, a
# name or a material - it is a bag of triangles - so it is checked for being
# readable rather than for being separable.

bpy.ops.wm.read_factory_settings(use_empty=True)
for index, part in enumerate(("leg_1", "leg_2", "top")):
    bpy.ops.mesh.primitive_cube_add(size=0.4, location=(index * 0.9, 0, 0))
    obj = bpy.context.active_object
    obj.name = part
    obj.data.name = part
    obj["forge_part_id"] = part.rsplit("_", 1)[0]

attempt("sep_blend", lambda: bpy.ops.wm.save_as_mainfile(
    filepath=f"{out}/m.blend", compress=True, copy=True))
attempt("sep_fbx", lambda: bpy.ops.export_scene.fbx(
    filepath=f"{out}/m.fbx", use_custom_props=True))
attempt("sep_obj", lambda: bpy.ops.wm.obj_export(
    filepath=f"{out}/m.obj", export_object_groups=False,
    export_material_groups=False))
attempt("sep_glb", lambda: bpy.ops.export_scene.gltf(
    filepath=f"{out}/m.glb", export_format="GLB", export_extras=True))

READERS = {
    "m.blend": lambda p: bpy.ops.wm.open_mainfile(filepath=p),
    "m.fbx": lambda p: bpy.ops.import_scene.fbx(filepath=p),
    "m.obj": lambda p: bpy.ops.wm.obj_import(filepath=p),
    "m.glb": lambda p: bpy.ops.import_scene.gltf(filepath=p),
}

for filename, read in READERS.items():
    path = f"{out}/{filename}"
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        failures.append(f"{filename}: nothing was written")
        continue
    try:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        read(path)
    except Exception as exc:
        failures.append(f"{filename}: could not be read back: "
                        f"{type(exc).__name__}: {exc}")
        continue

    names = sorted(o.name for o in bpy.context.scene.objects if o.type == "MESH")
    if len(names) != 3:
        failures.append(
            f"{filename}: three parts went in and {len(names)} came out "
            f"({names}) - parts are being merged, so nothing in this file can "
            f"be edited separately")
        continue
    # Names must survive too. Separate-but-anonymous objects are not editable
    # in any useful sense: there is no way to tell which one is the leg.
    if not all(any(n.startswith(p) for n in names) for p in ("leg", "top")):
        failures.append(f"{filename}: parts lost their names ({names})")

if failures:
    die("; ".join(failures))
print("SMOKE_FAILURES:none")
sys.exit(0)
