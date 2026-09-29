"""Render preview images of the built scene. Runs INSIDE Blender.

Rendered server-side rather than shown through a WebGL viewer in the browser.
That avoids shipping a 300KB library and a CDN dependency the page's CSP would
have to allow, works in any browser including old ones, and gives the project
list a thumbnail for free.

The Workbench engine is used deliberately: it is near-instant and draws clean
flat-shaded solids, which is what you want for checking that geometry is
correct. Cycles would spend a minute producing a prettier image that answers
the same question no better.
"""

from __future__ import annotations

import math
import os

import bpy
from mathutils import Euler, Vector

# Four views. The perspective one is for recognising the object; the three
# orthographic ones are for checking proportions, which is what someone
# verifying a generated model actually does.
VIEWS = {
    "perspective": {"kind": "persp", "rotation": (60, 0, 42)},
    "front": {"kind": "ortho", "rotation": (90, 0, 0)},
    "side": {"kind": "ortho", "rotation": (90, 0, 90)},
    "top": {"kind": "ortho", "rotation": (0, 0, 0)},
}

WIDTH, HEIGHT = 900, 700


def scene_bounds():
    """World-space bounding box of every mesh in the scene."""
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not meshes:
        return Vector((0, 0, 0)), Vector((1, 1, 1))

    lo = Vector((float("inf"),) * 3)
    hi = Vector((float("-inf"),) * 3)
    for obj in meshes:
        for corner in obj.bound_box:
            world = obj.matrix_world @ Vector(corner)
            for i in range(3):
                lo[i] = min(lo[i], world[i])
                hi[i] = max(hi[i], world[i])
    return lo, hi


def setup_world():
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = WIDTH
    scene.render.resolution_y = HEIGHT
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"

    shading = scene.display.shading
    # Object colour comes from the material, so parts are distinguishable -
    # the point of the preview is telling one piece from another.
    shading.light = "STUDIO"
    # TEXTURE rather than MATERIAL, so printed artwork actually appears.
    #
    # Workbench does not evaluate the shader graph, so a part carrying a Dove
    # wordmark rendered in its flat viewport colour and the logo was invisible
    # in exactly the picture someone checks it on - while the exported file had
    # it all along. TEXTURE draws the image where there is one and falls back
    # to the material colour where there is not, so every part that was right
    # before is unchanged.
    shading.color_type = "TEXTURE"
    shading.show_shadows = True
    shading.show_cavity = True
    shading.cavity_type = "BOTH"
    shading.studio_light = "Default"

    scene.display.render_aa = "8"

    # Show the colours that were asked for, not a photographic interpretation
    # of them.
    #
    # Blender 4's default view transform is AgX, a filmic tone mapper built for
    # rendered photography: it desaturates towards the highlights and lifts the
    # blacks, on purpose, because that is what makes a path-traced image look
    # like a photograph. Applied to a flat Workbench preview it is doing the
    # opposite of the job - the world background set two lines below as a near
    # white (0.93, 0.95, 0.97) came out as a muddy mid grey, and every material
    # colour read back several shades off what the specification said.
    #
    # That mattered much more once the colours started coming from the
    # photograph: someone checking a preview against the picture they uploaded
    # was comparing their object against a tone-mapped version of it and seeing
    # a difference that existed only in the render.
    #
    # Standard is a straight sRGB transfer - what the material says is what the
    # pixel gets.
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0

    # Workbench takes its render background from the *world* colour, not from
    # the viewport theme - and not from the world's shader nodes either. Both
    # are set here because getting it wrong renders on Blender's default dark
    # grey, which looks exactly like a failed render.
    shading.background_type = "WORLD"
    world = bpy.data.worlds.get("Forge") or bpy.data.worlds.new("Forge")
    world.use_nodes = False
    world.color = (0.93, 0.95, 0.97)
    scene.world = world


def add_camera(name: str, kind: str, rotation_degrees: tuple) -> object:
    data = bpy.data.cameras.new(name)
    data.type = "ORTHO" if kind == "ortho" else "PERSP"
    if kind == "persp":
        data.lens = 52.0

    camera = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(camera)
    camera.rotation_euler = tuple(math.radians(v) for v in rotation_degrees)
    return camera


def frame_camera(camera, lo: Vector, hi: Vector) -> None:
    """Point the camera at the object and pull back far enough to see all of it."""
    centre = (lo + hi) / 2.0
    size = hi - lo
    span = max(size.x, size.y, size.z, 0.05)

    # Derive the view direction from the euler we just set, NOT from
    # matrix_world. Blender only recomputes matrices on a depsgraph update, so
    # reading matrix_world here returns the identity and every camera ended up
    # pointing the same way - at nothing.
    direction = Euler(camera.rotation_euler).to_quaternion() @ Vector((0, 0, -1))

    # The frame is not square, and the camera's field of view is quoted across
    # its *longer* side. Fitting `span` to that alone fits the width of a
    # landscape render and says nothing about its height, so anything taller
    # than it is wide overflowed the top and bottom of the picture. A sideboard
    # is wider than it is tall and looked perfect; a chair had its castors and
    # the top of its backrest cut off. The aspect ratio has to be part of the
    # sum, not left out of it.
    render = bpy.context.scene.render
    width = max(1, render.resolution_x)
    height = max(1, render.resolution_y)
    long_over_short = max(width, height) / min(width, height)
    # How much wider the framing must be for the short side of the frame to
    # still contain `span`. 1.0 when the render is square.
    widen = long_over_short if height < width else 1.0

    if camera.data.type == "ORTHO":
        # A little margin, so the object is not cropped by rounding.
        camera.data.ortho_scale = span * 1.25 * widen
        distance = span * 3.0
    else:
        # Trigonometry rather than a guessed constant: how far back the camera
        # must sit for `span` to fit the field of view.
        fov = camera.data.angle
        distance = (span * 1.35 * widen) / (2.0 * math.tan(fov / 2.0))

    camera.location = centre - direction * distance


def render_views(out_dir: str, name: str) -> list[dict]:
    if not [o for o in bpy.context.scene.objects if o.type == "MESH"]:
        return []

    setup_world()
    lo, hi = scene_bounds()

    results = []
    scene = bpy.context.scene
    original_camera = scene.camera

    for view, config in VIEWS.items():
        camera = None
        try:
            camera = add_camera(f"cam_{view}", config["kind"], config["rotation"])
            frame_camera(camera, lo, hi)
            scene.camera = camera

            path = os.path.join(out_dir, f"{name}.{view}.png")
            scene.render.filepath = path
            bpy.ops.render.render(write_still=True)

            size = os.path.getsize(path) if os.path.exists(path) else 0
            if size:
                results.append({"view": view, "file": f"{name}.{view}.png",
                                "bytes": size})
        except Exception as exc:  # noqa: BLE001
            # A failed preview must never fail a build: the model is the
            # deliverable, the picture is a convenience.
            results.append({"view": view, "error": f"{type(exc).__name__}: {exc}"[:200]})
        finally:
            if camera is not None:
                bpy.data.objects.remove(camera, do_unlink=True)

    scene.camera = original_camera
    return results
