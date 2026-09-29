"""Rebuild a Blender scene from a `display_rack_json_v1` document.

    blender --background --factory-startup \
            --python scripts/import_rack_json.py -- model.json out.blend

This format is not a PartSpec. A PartSpec says what an object IS - parts,
measurements, where each one sits - and `forge/build/build.py` decides what
geometry that becomes. This document is the geometry: explicit vertices and
faces, materials with their shader values, images inline as base64, lights,
cameras and scene settings. It describes a finished model rather than the
thing the model is of.

So it gets its own importer rather than being fed through the builder. The two
are complementary: the builder produces an editable parametric model from a
reading of a photograph, and this reproduces a scene somebody has already
authored.

What is preserved: object names and collection tree, meshes with their UVs and
per-face material assignment, curves with their bevel, principled and emission
materials, embedded textures, modifiers, TRACK_TO constraints, lights, cameras
and the scene's units, engine and colour management.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile

import bpy


def die(message: str) -> None:
    print(f"IMPORT_ERROR: {message}")
    sys.exit(1)


def args() -> tuple[str, str]:
    argv = sys.argv
    rest = argv[argv.index("--") + 1:] if "--" in argv else []
    if len(rest) < 2:
        die("usage: import_rack_json.py -- <model.json> <out.blend>")
    return rest[0], rest[1]


# --------------------------------------------------------------------------- #
# Images and materials
# --------------------------------------------------------------------------- #

def load_images(document: dict, workdir: str) -> dict:
    """Decode every embedded PNG into a Blender image.

    Written to disk first and loaded from there rather than poked in as raw
    pixels: it is far faster for a 1473px graphic, and it leaves the exported
    .blend able to pack them by the normal route.
    """
    images = {}
    for name, entry in (document.get("images") or {}).items():
        data = entry.get("png_base64")
        if not data:
            continue
        path = os.path.join(workdir, f"{name}.png")
        try:
            with open(path, "wb") as handle:
                handle.write(base64.b64decode(data))
            image = bpy.data.images.load(path)
            image.name = name
            # Packed so the .blend is one self-contained file - the temporary
            # directory this was written to will not exist for whoever opens it.
            image.pack()
            images[name] = image
        except Exception as exc:  # noqa: BLE001
            print(f"IMPORT_WARN: image {name!r}: {type(exc).__name__}: {exc}")
    return images


def texture_node(material, image, extension: str = "REPEAT"):
    node = material.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    node.extension = extension
    node.location = (-420, 200)
    return node


def build_material(spec: dict, images: dict):
    """One material, principled or emission, with an optional image."""
    name = spec.get("name", "Material")
    material = bpy.data.materials.new(name=name)
    material.use_nodes = True
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links

    # Cleared by NAME, not by identity. Blender hands back a fresh Python
    # wrapper on every access, so `node is not output` is true even for the
    # node `output` refers to - which deleted the output along with everything
    # else and left every material failing on `output.inputs["Surface"]`.
    output_name = next((n.name for n in nodes if n.type == "OUTPUT_MATERIAL"), "")
    for name in [n.name for n in nodes if n.name != output_name]:
        nodes.remove(nodes[name])
    output = nodes.get(output_name) if output_name else None
    if output is None:
        output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (440, 0)

    image = images.get(spec.get("image")) if spec.get("image") else None
    extension = spec.get("extension", "REPEAT")
    alpha = bool(spec.get("alpha"))

    if spec.get("type") == "emission":
        shader = nodes.new("ShaderNodeEmission")
        shader.location = (0, 0)
        colour = spec.get("color") or spec.get("base_color") or [1, 1, 1]
        shader.inputs["Color"].default_value = (*colour[:3], 1.0)
        shader.inputs["Strength"].default_value = float(spec.get("strength", 1.0))
        if image is not None:
            tex = texture_node(material, image, extension)
            links.new(tex.outputs["Color"], shader.inputs["Color"])
            if alpha:
                # A lit logo is a shape on a transparent ground; without the
                # alpha routed to a mix it renders as a glowing rectangle.
                transparent = nodes.new("ShaderNodeBsdfTransparent")
                transparent.location = (0, -180)
                mix = nodes.new("ShaderNodeMixShader")
                mix.location = (220, 0)
                links.new(tex.outputs["Alpha"], mix.inputs["Fac"])
                links.new(transparent.outputs["BSDF"], mix.inputs[1])
                links.new(shader.outputs["Emission"], mix.inputs[2])
                links.new(mix.outputs["Shader"], output.inputs["Surface"])
                material.blend_method = "BLEND"
                _viewport(material, spec, colour)
                return material
        links.new(shader.outputs["Emission"], output.inputs["Surface"])
        _viewport(material, spec, colour)
        return material

    shader = nodes.new("ShaderNodeBsdfPrincipled")
    shader.location = (0, 0)
    colour = spec.get("base_color") or [0.8, 0.8, 0.8]
    _set(shader, "Base Color", (*colour[:3], 1.0))
    _set(shader, "Roughness", spec.get("roughness"))
    _set(shader, "Metallic", spec.get("metallic"))
    # Blender 4.x renamed these; try both so the file opens on either.
    _set(shader, "Transmission Weight", spec.get("transmission"), "Transmission")
    _set(shader, "Emission Strength", spec.get("emission_strength"))
    if spec.get("emission_color"):
        _set(shader, "Emission Color", (*spec["emission_color"][:3], 1.0),
             "Emission")

    if image is not None:
        tex = texture_node(material, image, extension)
        targets = spec.get("image_to") or ["base_color"]
        if "base_color" in targets:
            links.new(tex.outputs["Color"], shader.inputs["Base Color"])
        if "emission" in targets:
            for socket in ("Emission Color", "Emission"):
                if socket in shader.inputs:
                    links.new(tex.outputs["Color"], shader.inputs[socket])
                    break
        if alpha and "Alpha" in shader.inputs:
            links.new(tex.outputs["Alpha"], shader.inputs["Alpha"])
            material.blend_method = "BLEND"

    links.new(shader.outputs["BSDF"], output.inputs["Surface"])
    _viewport(material, spec, colour)
    return material


def _set(shader, socket: str, value, fallback: str = "") -> None:
    if value is None:
        return
    for name in (socket, fallback):
        if name and name in shader.inputs:
            shader.inputs[name].default_value = value
            return


def _viewport(material, spec: dict, colour) -> None:
    """The solid-mode colour, which is what Workbench previews draw."""
    viewport = spec.get("viewport_color") or colour
    material.diffuse_color = (*viewport[:3], 1.0)
    if spec.get("roughness") is not None:
        material.roughness = float(spec["roughness"])
    if spec.get("metallic") is not None:
        material.metallic = float(spec["metallic"])


# --------------------------------------------------------------------------- #
# Objects
# --------------------------------------------------------------------------- #

def make_mesh(spec: dict, materials: dict):
    mesh = bpy.data.meshes.new(spec["name"])
    verts = [tuple(v) for v in spec.get("verts", [])]
    faces = [tuple(f) for f in spec.get("faces", [])]
    mesh.from_pydata(verts, [], faces)
    mesh.update()

    for name in spec.get("materials", []):
        mesh.materials.append(materials.get(name))

    indices = spec.get("mat_index") or []
    smooth = spec.get("smooth") or []
    for i, polygon in enumerate(mesh.polygons):
        if i < len(indices):
            polygon.material_index = indices[i]
        if i < len(smooth):
            polygon.use_smooth = bool(smooth[i])

    uvs = spec.get("uvs")
    if uvs:
        layer = mesh.uv_layers.new(name="UVMap")
        for face_index, polygon in enumerate(mesh.polygons):
            if face_index >= len(uvs):
                break
            corners = uvs[face_index]
            for corner, loop_index in enumerate(polygon.loop_indices):
                if corner < len(corners):
                    layer.data[loop_index].uv = corners[corner]

    return bpy.data.objects.new(spec["name"], mesh)


def make_curve(spec: dict, materials: dict):
    curve = bpy.data.curves.new(spec["name"], type="CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = float(spec.get("bevel_depth", 0.0))
    curve.bevel_resolution = int(spec.get("bevel_resolution", 0))

    for points in spec.get("splines", []):
        if len(points) < 2:
            continue
        spline = curve.splines.new("POLY")
        spline.points.add(len(points) - 1)
        for point, (x, y, z) in zip(spline.points, points):
            point.co = (x, y, z, 1.0)

    for name in spec.get("materials", []):
        curve.materials.append(materials.get(name))
    return bpy.data.objects.new(spec["name"], curve)


LIGHT_TYPES = {"AREA", "POINT", "SUN", "SPOT"}


def make_light(spec: dict):
    kind = str(spec.get("light_type", "POINT")).upper()
    data = bpy.data.lights.new(spec["name"],
                               type=kind if kind in LIGHT_TYPES else "POINT")
    data.energy = float(spec.get("energy", 100.0))
    if spec.get("color"):
        data.color = tuple(spec["color"][:3])
    if spec.get("size") is not None and hasattr(data, "size"):
        data.size = float(spec["size"])
    return bpy.data.objects.new(spec["name"], data)


def make_camera(spec: dict):
    data = bpy.data.cameras.new(spec["name"])
    data.lens = float(spec.get("lens", 50.0))
    return bpy.data.objects.new(spec["name"], data)


BUILDERS = {
    "MESH": lambda s, m: make_mesh(s, m),
    "CURVE": lambda s, m: make_curve(s, m),
    "LIGHT": lambda s, m: make_light(s),
    "CAMERA": lambda s, m: make_camera(s),
    "EMPTY": lambda s, m: bpy.data.objects.new(s["name"], None),
}


def add_modifiers(obj, spec: dict) -> None:
    for entry in spec.get("modifiers", []):
        kind = str(entry.get("type", "")).upper()
        if kind != "BEVEL":
            print(f"IMPORT_WARN: {obj.name}: unsupported modifier {kind!r}")
            continue
        modifier = obj.modifiers.new(name=entry.get("name", "Bevel"),
                                     type="BEVEL")
        if entry.get("width") is not None:
            modifier.width = float(entry["width"])
        if entry.get("segments") is not None:
            modifier.segments = int(entry["segments"])
        if entry.get("limit_method"):
            modifier.limit_method = entry["limit_method"]
        if entry.get("angle_limit") is not None:
            modifier.angle_limit = float(entry["angle_limit"])


def add_constraints(obj, spec: dict, objects: dict) -> None:
    """Applied after every object exists, so a target can be resolved."""
    for entry in spec.get("constraints", []):
        if str(entry.get("type", "")).upper() != "TRACK_TO":
            continue
        target = objects.get(entry.get("target"))
        if target is None:
            print(f"IMPORT_WARN: {obj.name}: no target {entry.get('target')!r}")
            continue
        constraint = obj.constraints.new("TRACK_TO")
        constraint.target = target
        if entry.get("track_axis"):
            constraint.track_axis = entry["track_axis"]
        if entry.get("up_axis"):
            constraint.up_axis = entry["up_axis"]


# --------------------------------------------------------------------------- #
# Scene
# --------------------------------------------------------------------------- #

def apply_scene(document: dict, objects: dict) -> None:
    scene = bpy.context.scene
    settings = document.get("scene") or {}

    if settings.get("unit_system"):
        scene.unit_settings.system = settings["unit_system"]
    if settings.get("length_unit"):
        try:
            scene.unit_settings.length_unit = settings["length_unit"]
        except (TypeError, ValueError):
            pass

    engine = settings.get("render_engine")
    if engine:
        try:
            scene.render.engine = engine
        except (TypeError, ValueError):
            print(f"IMPORT_WARN: engine {engine!r} unavailable; keeping "
                  f"{scene.render.engine}")
    if scene.render.engine == "CYCLES" and settings.get("samples"):
        scene.cycles.samples = int(settings["samples"])

    if settings.get("view_transform"):
        scene.view_settings.view_transform = settings["view_transform"]
    if settings.get("look"):
        scene.view_settings.look = settings["look"]
    if settings.get("exposure") is not None:
        scene.view_settings.exposure = float(settings["exposure"])

    resolution = settings.get("resolution")
    if resolution and len(resolution) == 2:
        scene.render.resolution_x, scene.render.resolution_y = resolution

    camera = objects.get(settings.get("camera"))
    if camera is not None:
        scene.camera = camera

    world = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    if background is not None:
        colour = settings.get("world_color") or [0.05, 0.05, 0.05]
        background.inputs["Color"].default_value = (*colour[:3], 1.0)
        background.inputs["Strength"].default_value = float(
            settings.get("world_strength", 1.0))
    scene.world = world


def meshify(objects: dict) -> list[str]:
    """Convert curve objects to meshes before the file is saved.

    A great many online .blend viewers read mesh objects and silently skip
    everything else. This document's gold veins are curves - fourteen of them -
    so a rack uploaded to a web viewer arrives with its kintsugi missing and no
    error to explain it. The GLB export happens to convert them on the way out,
    which is why the web viewer looked right while the .blend did not: two
    different files disagreeing about what the model contains.

    The bevel is what gives a vein its thickness, so the conversion has to keep
    it; `object.convert` does, because it evaluates the object as rendered.
    Name, collection and materials are preserved, so nothing above this notices
    the change.
    """
    curves = [o for o in objects.values() if o.type == "CURVE"]
    if not curves:
        return []

    converted = []
    previous = bpy.context.view_layer.objects.active
    bpy.ops.object.select_all(action="DESELECT")
    for obj in curves:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = curves[0]
    try:
        bpy.ops.object.convert(target="MESH")
        converted = [o.name for o in curves]
    except Exception as exc:  # noqa: BLE001
        print(f"IMPORT_WARN: could not convert curves to mesh: "
              f"{type(exc).__name__}: {exc}")
    finally:
        bpy.ops.object.select_all(action="DESELECT")
        if previous is not None and previous.name in bpy.data.objects:
            bpy.context.view_layer.objects.active = previous
    return converted


def main() -> None:
    source, destination = args()
    try:
        with open(source) as handle:
            document = json.load(handle)
    except Exception as exc:  # noqa: BLE001
        die(f"cannot read {source}: {type(exc).__name__}: {exc}")

    fmt = document.get("format", "")
    if not fmt.startswith("display_rack_json"):
        die(f"unexpected format {fmt!r}")

    bpy.ops.wm.read_factory_settings(use_empty=True)

    with tempfile.TemporaryDirectory(prefix="rack-images-") as workdir:
        images = load_images(document, workdir)

        materials = {}
        for spec in document.get("materials", []):
            try:
                materials[spec["name"]] = build_material(spec, images)
            except Exception as exc:  # noqa: BLE001
                print(f"IMPORT_WARN: material {spec.get('name')!r}: "
                      f"{type(exc).__name__}: {exc}")

        # Collections first, parents before children, so every object has
        # somewhere to be linked as it is created.
        collections = {}
        for entry in document.get("collections", []):
            collections[entry["name"]] = bpy.data.collections.new(entry["name"])
        for entry in document.get("collections", []):
            child = collections[entry["name"]]
            parent = collections.get(entry.get("parent"))
            (parent or bpy.context.scene.collection).children.link(child)

        objects, skipped = {}, []
        for spec in document.get("objects", []):
            builder = BUILDERS.get(spec.get("type"))
            if builder is None:
                skipped.append((spec.get("name"), spec.get("type")))
                continue
            try:
                obj = builder(spec, materials)
            except Exception as exc:  # noqa: BLE001
                print(f"IMPORT_WARN: object {spec.get('name')!r}: "
                      f"{type(exc).__name__}: {exc}")
                skipped.append((spec.get("name"), spec.get("type")))
                continue

            obj.location = tuple(spec.get("location") or (0, 0, 0))
            obj.rotation_euler = tuple(spec.get("rotation") or (0, 0, 0))
            obj.scale = tuple(spec.get("scale") or (1, 1, 1))
            add_modifiers(obj, spec)

            target = collections.get(spec.get("collection"))
            (target or bpy.context.scene.collection).objects.link(obj)
            objects[spec["name"]] = obj

        for spec in document.get("objects", []):
            obj = objects.get(spec.get("name"))
            if obj is not None:
                add_constraints(obj, spec, objects)

        apply_scene(document, objects)
        converted = meshify(objects)

        bpy.ops.wm.save_as_mainfile(filepath=destination, compress=True)

    meshes = sum(1 for o in objects.values() if o.type == "MESH")
    print(f"IMPORT_OK objects={len(objects)} meshes={meshes} "
          f"materials={len(materials)} images={len(images)} "
          f"collections={len(collections)} skipped={len(skipped)} "
          f"curves_meshed={len(converted)}")
    for name, kind in skipped:
        print(f"IMPORT_SKIPPED {name!r} ({kind})")


if __name__ == "__main__":
    main()
