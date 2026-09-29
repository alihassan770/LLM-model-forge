"""Print artwork onto a face. Runs INSIDE Blender.

A retail display is mostly print. The Dove stand in the reference photograph is
a white box until the wordmark is on the header, a blank panel until the model
photograph is in the lightbox, and four anonymous rails until "HAIR REBORN 10x
STRONGER" is on the shelf talkers. The vision model reads all of that correctly
and named every one of them - and before this module every one became a
flat-coloured box, because there was nowhere for an image to go.

The artwork is cut from the uploaded picture itself, so what lands on the model
is the real logo and the real photograph rather than an approximation of them.

Two deliberate limits:

* One image per part, on one face. A part printed on two faces is two parts,
  which is also how it is made.
* The image is projected flat onto the face, not wrapped. Everything this
  handles is printed on a flat panel, and a projection that pretends to follow
  a curve would be confidently wrong about where the logo sits.
"""

from __future__ import annotations

import os

import bpy
from mathutils import Vector

# Which way each named face points, in the part's own frame - matching the
# object frame used everywhere else: +X right, +Y back, +Z up.
FACE_NORMALS = {
    "front": Vector((0.0, -1.0, 0.0)),
    "back": Vector((0.0, 1.0, 0.0)),
    "left": Vector((-1.0, 0.0, 0.0)),
    "right": Vector((1.0, 0.0, 0.0)),
    "top": Vector((0.0, 0.0, 1.0)),
    "bottom": Vector((0.0, 0.0, -1.0)),
}

# Which two extents span each face, shared with the runner so that the padding
# it applies and the projection here cannot disagree about which face is which.
from forge.spec.schema import FACE_AXES  # noqa: E402

# How square-on a polygon must face the named direction to count as part of
# that face. Generous, because a bevelled panel's front is made of several
# slightly-tilted polygons and the artwork belongs on all of them.
FACING_TOLERANCE = 0.55


def _facing(obj, face: str) -> list:
    """The polygons that make up one named face of a part."""
    normal = FACE_NORMALS.get(face)
    if normal is None:
        return []
    return [p for p in obj.data.polygons if p.normal.dot(normal) >= FACING_TOLERANCE]


def apply(obj, face: str, image_path: str, describes: str = "") -> str:
    """Print one image onto one face of one object.

    Returns an empty string on success, or a reason it could not be done - a
    caller must never let a missing logo cost the geometry.
    """
    if not image_path or not os.path.exists(image_path):
        return f"artwork file is missing: {image_path}"

    polygons = _facing(obj, face)
    if not polygons:
        return f"no polygons face {face!r}"

    try:
        image = bpy.data.images.load(image_path, check_existing=True)
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"

    material = bpy.data.materials.new(name=f"{obj.name}_{face}_art")
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links

    bsdf = nodes.get("Principled BSDF")
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = image
    # The artwork is a photograph of a printed panel, so it is already the
    # colour it should appear - not a value to be interpreted.
    texture.interpolation = "Smart"
    texture.extension = "CLIP"

    if bsdf is not None:
        links.new(texture.outputs["Color"], bsdf.inputs["Base Color"])
        # Printed panels are not shiny and are never metal; leaving the
        # defaults would put a plastic sheen over a paper logo.
        bsdf.inputs["Roughness"].default_value = 0.55
        bsdf.inputs["Metallic"].default_value = 0.0

    # Workbench - the engine the previews use - does not evaluate the shader
    # graph, so a textured part would render in the material's flat viewport
    # colour and the artwork would be invisible in exactly the picture someone
    # checks it on. Telling the viewport to show the texture is what makes the
    # preview and the exported file agree.
    material.diffuse_color = (1.0, 1.0, 1.0, 1.0)

    slot = len(obj.data.materials)
    obj.data.materials.append(material)

    # Project the image flat across the face, so it lands exactly where the
    # region said it does rather than wherever an automatic unwrap put it.
    mesh = obj.data
    if not mesh.uv_layers:
        mesh.uv_layers.new(name="artwork")
    uv_layer = mesh.uv_layers.active

    across, up = FACE_AXES[face]
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    lo_a = min(c[across] for c in corners)
    hi_a = max(c[across] for c in corners)
    lo_u = min(c[up] for c in corners)
    hi_u = max(c[up] for c in corners)
    span_a = (hi_a - lo_a) or 1.0
    span_u = (hi_u - lo_u) or 1.0

    # A face seen from behind reads right-to-left, so its U runs backwards -
    # otherwise every logo on a back or a left face comes out mirrored.
    flip = face in ("back", "left", "bottom")

    for polygon in polygons:
        polygon.material_index = slot
        for loop_index in polygon.loop_indices:
            world = obj.matrix_world @ mesh.vertices[
                mesh.loops[loop_index].vertex_index].co
            u = (world[across] - lo_a) / span_a
            v = (world[up] - lo_u) / span_u
            uv_layer.data[loop_index].uv = (1.0 - u if flip else u, v)

    if describes:
        obj["forge_artwork"] = describes[:60]
    obj["forge_artwork_face"] = face
    return ""
