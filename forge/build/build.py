"""Turn a PartSpec into Blender geometry. Runs INSIDE Blender.

Invoked as:

    blender --background --factory-startup \
            --python forge/build/build.py -- --spec spec.json --out outdir

`--factory-startup` matters: it ignores any user preferences and add-ons, so a
build is identical on a developer's laptop and in the worker container. Without
it, a stray add-on can change export behaviour and the difference only shows up
in a customer's file.

No code from a model is executed here. The specification is data; this file is
the only thing that decides what geometry exists, and it is reviewed and
version-controlled like any other source.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import bpy  # noqa: F401 - provided by the Blender runtime

# Importing a sibling module requires the repository on sys.path, because
# Blender runs this file as a script rather than as part of the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from forge.build import naming  # noqa: E402
from forge.build import profiles  # noqa: E402
from forge.spec import dims as spec_dims  # noqa: E402
from forge.spec import schema as spec_schema  # noqa: E402
from forge.build.layout import place  # noqa: E402

# Blender works in metres; the specification is in millimetres. One conversion,
# in one place, applied on the way in.
MM = 0.001


# --------------------------------------------------------------------------- #
# Dimension reading
# --------------------------------------------------------------------------- #

# Dimension resolution lives in forge/spec/dims.py, shared with the bill of
# materials. They previously each had their own copy and disagreed: a leg was
# built 70×70×735mm and costed as 800×735×70mm. Sharing the function is what
# makes "the bill cannot disagree with the model" structural.
dims_mm = spec_dims.axes
box_extent = spec_dims.box_extent

# These two lines used to be followed by a second, private copy of the same
# functions, which redefined the names and won. The copies had drifted: given a
# handle measured as "length 160", "diameter 12" and "handle length and fixing
# centres 160", dims.py recognised the third as another length and ignored it,
# while the copy here filed it as an unknown extra and made it the height. The
# handle was built as a 160mm post standing on the drawer front. Keep the
# resolution in dims.py; a copy here is a second answer waiting to diverge.


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #

def make_box(name: str, part: dict):
    x, y, z = box_extent(part)
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = (x * MM, y * MM, z * MM)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return obj


def make_cylinder(name: str, part: dict, vertices: int = 48):
    """A round bar, lying along whichever axis it is longest on.

    Blender builds a cylinder standing up the Z axis, and leaving it there made
    every round part vertical regardless of its dimensions: a 160mm bar handle
    12mm thick came out as a 160mm post sticking out of the drawer front. The
    layout code, meanwhile, reasons about the part through box_extent, which
    had already decided that handle was 160 long on X.

    So the axis is taken from box_extent rather than guessed again here. That
    keeps one answer to "which way does this part run" - the primitive and the
    placement can no longer disagree. The rotation is applied into the mesh, so
    whatever the layout sets afterwards still composes on top of it.
    """
    d = dims_mm(part)
    diameter, length = spec_dims.cylinder_extent(part)
    if not diameter:
        diameter = d.get("width") or 50.0

    bpy.ops.mesh.primitive_cylinder_add(vertices=vertices, radius=1.0, depth=1.0)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = (diameter / 2 * MM, diameter / 2 * MM, length * MM)

    # Which way it lies comes from `dims.round_axis`, the same function
    # `box_extent` uses. Deciding it here independently is how the builder and
    # the bill of materials drift apart, and this module exists because they
    # did once. A placement naming a `run_axis` overrides this afterwards, in
    # `anchors.orient`.
    axial = spec_dims.round_axis(d)
    if axial is not None and axial[1] == "x":
        obj.rotation_euler = (0.0, math.radians(90), 0.0)   # lay it along X

    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    return obj


def make_tube(name: str, part: dict):
    """A hollow section, built as a cylinder with a solidify modifier.

    The modifier is left live rather than applied: a downstream editor can
    change the wall thickness, which is the whole point of shipping an editable
    file rather than a finished mesh.
    """
    obj = make_cylinder(name, part)
    d = dims_mm(part)
    wall = d.get("thickness") or max(1.5, (d.get("diameter", 40.0) * 0.06))
    modifier = obj.modifiers.new(name="Wall", type="SOLIDIFY")
    modifier.thickness = wall * MM
    modifier.offset = -1.0
    return obj


def make_sphere(name: str, part: dict):
    d = dims_mm(part)
    diameter = d.get("diameter") or (d.get("radius", 25.0) * 2)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=1.0)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = tuple([diameter / 2 * MM] * 3)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return obj


def make_cushion(name: str, part: dict):
    """A box with a live bevel, which is what a cushion actually is."""
    obj = make_box(name, part)
    x, y, z = box_extent(part)
    modifier = obj.modifiers.new(name="Softness", type="BEVEL")
    modifier.width = min(x, y, z) * 0.22 * MM
    modifier.segments = 4
    modifier.limit_method = "ANGLE"
    subsurf = obj.modifiers.new(name="Smooth", type="SUBSURF")
    subsurf.levels = 1
    subsurf.render_levels = 2
    return obj


def make_revolve(name: str, part: dict):
    """A turned part - a profile spun about its axis.

    With a silhouette from the specification this is a real lathe operation and
    the result is the actual turning. Without one it falls back to a cylinder
    and says so in the object's custom properties, so nobody mistakes an
    approximation for a reading.
    """
    profile = spec_schema.shape_of(part)["profile"]
    if profile:
        diameter, length = spec_dims.cylinder_extent(part)
        obj = profiles.make_revolved(name, profile, diameter, length)
        obj["forge_profile_points"] = len(profile)
        return obj

    obj = make_cylinder(name, part, vertices=32)
    obj["forge_approximated"] = "revolve without a profile; tapered cylinder"
    return obj


def make_sweep(name: str, part: dict):
    """A bent member: a section carried along a drawn curve.

    Falls back to a box when no path was given, because a sweep without a
    curve is a straight member and a box is the honest way to build one - and
    a build that fails is worse than a build that simplifies.
    """
    shape = spec_schema.shape_of(part)
    extent = spec_dims.box_extent(part)
    if not shape["path"]:
        obj = make_box(name, part)
        obj["forge_approximated"] = "sweep without a path; built as a box"
        return obj

    d = dims_mm(part)
    diameter = d.get("diameter") or (d.get("radius", 0) * 2)
    if diameter:
        section = (diameter, diameter)
        round_section = True
    else:
        # The two smallest sides are the section; the curve accounts for the
        # length, so a swept part's longest measurement is its reach, not its
        # thickness.
        sides = sorted(extent)
        section = (sides[1], sides[0])
        round_section = False

    return profiles.make_swept(
        name, shape["path"], shape["path_plane"], shape["path_closed"],
        extent, section, round_section,
    )


BUILDERS = {
    "sweep": make_sweep,
    "box": make_box,
    "cylinder": make_cylinder,
    "tube": make_tube,
    "sphere": make_sphere,
    "cushion": make_cushion,
    "revolve": make_revolve,
    "extrude": make_box,
}


# --------------------------------------------------------------------------- #
# Materials
# --------------------------------------------------------------------------- #

# Plausible base colours so a preview is readable. Real materials are a later
# concern; what matters now is that slots exist and are named after the BOM
# codes, so the two never drift apart.
MATERIAL_COLOURS = {
    "wood": (0.44, 0.27, 0.14, 1.0),
    "oak": (0.62, 0.45, 0.26, 1.0),
    "walnut": (0.28, 0.17, 0.10, 1.0),
    "pine": (0.76, 0.60, 0.38, 1.0),
    "mdf": (0.66, 0.55, 0.42, 1.0),
    "steel": (0.62, 0.64, 0.67, 1.0),
    "metal": (0.62, 0.64, 0.67, 1.0),
    "aluminium": (0.75, 0.76, 0.78, 1.0),
    "chrome": (0.85, 0.87, 0.90, 1.0),
    "plastic": (0.20, 0.20, 0.22, 1.0),
    "abs": (0.20, 0.20, 0.22, 1.0),
    "nylon": (0.15, 0.15, 0.16, 1.0),
    "fabric": (0.55, 0.53, 0.50, 1.0),
    "linen": (0.72, 0.69, 0.62, 1.0),
    "leather": (0.35, 0.22, 0.16, 1.0),
    "foam": (0.90, 0.88, 0.80, 1.0),
    "glass": (0.80, 0.86, 0.88, 0.25),

    # Names the model actually writes that the list above did not cover, so
    # the parts fell back to the default grey. A chair spec said
    # "pp_glassfilled" for its base and "fabric_polyester" for its seat: the
    # second matched, the first did not, and half the chair was the colour of
    # nothing in particular.
    "polypropylene": (0.22, 0.22, 0.24, 1.0),
    "pp": (0.22, 0.22, 0.24, 1.0),
    "polyester": (0.52, 0.51, 0.49, 1.0),
    "polyamide": (0.18, 0.18, 0.20, 1.0),
    "pom": (0.26, 0.26, 0.28, 1.0),
    "pvc": (0.30, 0.30, 0.32, 1.0),
    "polyurethane": (0.88, 0.86, 0.80, 1.0),
    "rubber": (0.12, 0.12, 0.13, 1.0),
    "brass": (0.72, 0.58, 0.28, 1.0),
    "bronze": (0.55, 0.40, 0.22, 1.0),
    "iron": (0.28, 0.28, 0.30, 1.0),
    "zinc": (0.70, 0.72, 0.74, 1.0),
    "beech": (0.82, 0.68, 0.48, 1.0),
    "ash": (0.80, 0.72, 0.56, 1.0),
    "birch": (0.85, 0.76, 0.58, 1.0),
    "maple": (0.83, 0.71, 0.52, 1.0),
    "teak": (0.55, 0.38, 0.20, 1.0),
    "mahogany": (0.40, 0.20, 0.13, 1.0),
    "plywood": (0.72, 0.58, 0.38, 1.0),
    "chipboard": (0.64, 0.54, 0.40, 1.0),
    "veneer": (0.60, 0.44, 0.26, 1.0),
    "marble": (0.88, 0.88, 0.86, 1.0),
    "stone": (0.58, 0.57, 0.55, 1.0),
    "ceramic": (0.90, 0.89, 0.86, 1.0),
    "velvet": (0.35, 0.22, 0.34, 1.0),
    "cotton": (0.78, 0.75, 0.70, 1.0),
    "wool": (0.52, 0.50, 0.46, 1.0),
    "mesh": (0.24, 0.24, 0.26, 1.0),
}

# Family names, consulted only when no specific material matched. Everything
# here is a category a real material belongs to rather than a material anyone
# would actually specify, so if one of these is the best match the spec simply
# did not say which kind.
GENERIC_MATERIALS = frozenset({
    "wood", "metal", "plastic", "fabric", "stone",
})


# How a stated finish behaves, as (metallic, roughness). Read off the model's
# one-word answer instead of guessed from the material code, because the finish
# is a thing you can see and the material code is a thing you infer: a powder
# coated steel leg and a polished steel leg carry the same code and look
# nothing alike.
FINISH_SHADING = {
    "matte": (0.0, 0.92),
    "satin": (0.0, 0.55),
    "gloss": (0.0, 0.14),
    "metallic": (0.9, 0.32),
    "transparent": (0.0, 0.05),
    "textile": (0.0, 0.95),
    "raw": (0.0, 0.80),
}


def srgb_to_linear(channel: float) -> float:
    """A colour picked off a photograph into the space Blender shades in.

    Skipping this is not a subtlety: a mid-grey #808080 handed straight to
    Blender renders at roughly double its intended lightness, so every colour
    the model reads comes out washed out and pale. The two spaces differ most
    exactly where furniture lives - the mid tones.
    """
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


# Keyed by material code *and* colour. Two parts sharing a code but not a
# colour - a cabinet with an oak carcass and a painted oak door - are two
# materials, and caching on the code alone silently gave the second part the
# first one's colour.
_MATERIALS: dict[str, object] = {}


def material_for(part: dict):
    """The material for one part: its observed colour where there is one.

    The keyword table below is the fallback, not the source of truth. It maps a
    material code onto the colour that kind of material usually is, which is
    the best guess available when nobody has looked - and a guess is all it can
    ever be, because it cannot know the oak was painted black.
    """
    code = (part.get("material") or "unspecified").strip().lower()
    look = spec_schema.appearance_of(part)

    key = f"{code}@{look['hex'] or '-'}@{look['finish'] or '-'}"
    cached = _MATERIALS.get(key)
    if cached is not None:
        return cached

    observed = look["rgb"]
    # "glass-filled" nylon and polypropylene are opaque engineering plastics
    # that happen to contain the word glass. Treating them as glass made a
    # chair's seat pan, back support and arms into pale transparent sheets.
    # The exclusion has to apply to the colour as much as to the alpha, which
    # is why it is decided here, before the lookup, rather than after it.
    glass_filled = "glass" in code and ("filled" in code or "fill" in code)

    colour = (0.55, 0.55, 0.58, 1.0)
    # The specific name wins over the family name. "hardwood_oak" contains both
    # "wood" and "oak", and "metal_brass" contains both "metal" and "brass" -
    # matching in dictionary order gave the family, so every oak part was
    # generic brown and every brass handle was grey. The more precisely the
    # model described a material, the less of that description was used.
    #
    # Sorting by length does not fix it, because "wood" is longer than "oak".
    # What distinguishes them is not length but rank: a family name is a
    # fallback for when no species is named, so it is only consulted after all
    # of them have failed.
    for keyword in sorted(MATERIAL_COLOURS, key=len, reverse=True):
        if keyword in GENERIC_MATERIALS:
            continue
        if keyword == "glass" and glass_filled:
            continue
        if keyword in code:
            colour = MATERIAL_COLOURS[keyword]
            break
    else:
        for keyword in sorted(GENERIC_MATERIALS, key=len, reverse=True):
            if keyword in code:
                colour = MATERIAL_COLOURS[keyword]
                break

    # An observed colour replaces the guess outright. This is the entire point
    # of asking: the table above knows what oak looks like, and only the
    # photograph knows what THIS oak was finished with.
    if observed is not None:
        colour = (srgb_to_linear(observed[0]), srgb_to_linear(observed[1]),
                  srgb_to_linear(observed[2]), colour[3])

    material = bpy.data.materials.new(name=code)
    material.use_nodes = True

    transparent = ("glass" in code and not glass_filled) or \
        look["finish"] == "transparent"

    # A stated finish wins over what the code implies, for the same reason an
    # observed colour does. The keyword tests remain as the fallback for a spec
    # that carries no appearance at all.
    if look["finish"] in FINISH_SHADING:
        metallic_value, roughness = FINISH_SHADING[look["finish"]]
    else:
        metallic = any(k in code for k in
                       ("steel", "metal", "chrome", "aluminium", "brass", "iron"))
        soft = any(k in code for k in
                   ("fabric", "linen", "foam", "leather", "velvet", "upholst"))
        metallic_value = 0.9 if metallic else 0.0
        roughness = 0.35 if metallic else (0.9 if soft else 0.5)

    alpha = 0.25 if transparent else 1.0

    # Workbench - the engine the previews use - shades from the material's
    # *viewport* colour, not from the Principled BSDF. Setting only the BSDF
    # left every preview in the default grey no matter what the spec said the
    # part was made of, while the exported .blend carried the right colours all
    # along. Both are set here: the viewport colour is what the preview shows
    # and the BSDF is what any renderer downstream will use.
    material.diffuse_color = (colour[0], colour[1], colour[2], alpha)
    material.metallic = metallic_value
    material.roughness = roughness

    bsdf = material.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (colour[0], colour[1],
                                                   colour[2], 1.0)
        bsdf.inputs["Metallic"].default_value = metallic_value
        bsdf.inputs["Roughness"].default_value = roughness
        if transparent:
            bsdf.inputs["Alpha"].default_value = alpha
            material.blend_method = "BLEND"

    _MATERIALS[key] = material
    return material


# --------------------------------------------------------------------------- #
# Scene assembly
# --------------------------------------------------------------------------- #

def clear_scene():
    # The material cache holds references into the datablocks being removed
    # below. Left populated across a build it hands back materials that no
    # longer exist, which Blender reports much later and somewhere else.
    _MATERIALS.clear()
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in list(bpy.data.collections):
        bpy.data.collections.remove(collection)
    for block in (bpy.data.meshes, bpy.data.materials, bpy.data.objects):
        for item in list(block):
            if item.users == 0:
                block.remove(item)


def get_collection(name: str):
    existing = bpy.data.collections.get(name)
    if existing is not None:
        return existing
    collection = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(collection)
    return collection


def move_to(obj, collection):
    for current in list(obj.users_collection):
        current.objects.unlink(obj)
    collection.objects.link(obj)


def build(spec: dict, revision: int, art_files: dict | None = None) -> dict:
    """Assemble the object. `art_files` maps a part id to a cropped PNG path,
    prepared by the runner from the regions the model named."""
    clear_scene()

    names = naming.Names()
    parts = spec.get("parts", [])

    # Assemble the object the way that measures best against its own stated
    # dimensions, rather than the way a hand-picked threshold guesses at. Falls
    # back to the plain assembly if anything here goes wrong: choosing well is
    # an improvement, and building at all is the job.
    strategy = "automatic"
    checks: list[dict] = []
    try:
        from forge.build import audit  # noqa: PLC0415
        strategy, placements, checks = audit.best_placements(spec)
    except Exception:  # noqa: BLE001
        placements = place(spec)

    built, skipped, warnings = [], [], []
    by_part: dict[str, list] = {}

    for part in parts:
        builder = BUILDERS.get(part.get("primitive", "box"))
        if builder is None:
            skipped.append({"id": part["id"], "why": "unknown primitive"})
            continue

        count = max(1, int(part.get("count", 1)))
        material = material_for(part)
        collection = get_collection(naming.collection_for(part))
        shape = spec_schema.shape_of(part)
        extent = spec_dims.box_extent(part)
        instances = []

        # A silhouette only means something on a lathe. Reported rather than
        # ignored, because a profile on a box is a spec worth correcting and a
        # silent drop is how it stays wrong.
        if shape["profile"] and part.get("primitive") != "revolve":
            warnings.append({
                "id": part["id"],
                "why": f"a turned profile was given for a "
                       f"{part.get('primitive')}, and only revolve parts "
                       f"can be lathed",
            })

        for index in range(count):
            name = names.instance(part["id"], index, count)
            try:
                obj = builder(name, part)
            except Exception as exc:  # noqa: BLE001
                skipped.append({"id": part["id"], "why": f"{type(exc).__name__}: {exc}"})
                break

            # Taper and edge rounding are applied per instance, before the
            # placement rotation goes on: both work in the part's own frame,
            # where "the long axis" and "the narrow end" mean something.
            #
            # Shaping must never cost the geometry, for the same reason the
            # metadata below must not. A part that is the right size and
            # sharp-edged is a small disappointment; a part that failed to
            # build is a missing leg.
            try:
                # Taper scales a part's cross-section along its longest
                # local axis, which is meaningless for a curve: the long
                # axis of a bent loop runs through empty space, so the
                # scaling would pinch the geometry across the hole rather
                # than narrow the member. A swept part's section is
                # constant by construction.
                if part.get("primitive") != "sweep":
                    profiles.apply_taper(obj, extent, shape["taper"])
                profiles.apply_edge_radius(obj, extent, shape["edge_radius_mm"])
            except Exception as exc:  # noqa: BLE001
                warnings.append({
                    "id": part["id"],
                    "why": f"shaping skipped: {type(exc).__name__}: {exc}",
                })

            # Name the mesh datablock too, not just the object. Several
            # exporters build their group names from the mesh, so leaving it as
            # "Cube.003" produces groups like `leg_1_Cube.003` and the link
            # back to the spec is lost in exactly the formats people open.
            obj.data.name = name

            obj.data.materials.clear()
            obj.data.materials.append(material)

            # Metadata must never cost the geometry. These are annotations on
            # a part that is already built correctly, and Blender rejects a bad
            # property name by raising, so one unexpected key used to abort the
            # entire build after every object had been made. The names are
            # bounded in naming.property_key; this is the second line, because
            # losing a model over a label is the wrong trade either way.
            for key, value in naming.properties_for(part, revision, index).items():
                try:
                    obj[key] = value
                except Exception as exc:  # noqa: BLE001
                    warnings.append({
                        "id": part["id"],
                        "why": f"property {key!r} not set: "
                               f"{type(exc).__name__}: {exc}",
                    })

            # Printed artwork, cut from the uploaded picture. Applied before
            # the part is moved into place, because the projection works in
            # the part's own frame - and never allowed to cost the geometry:
            # a missing logo is a disappointment, a missing panel is a hole.
            art_path = (art_files or {}).get(part["id"])
            if art_path:
                art = spec_schema.artwork_of(part)
                if art is not None:
                    try:
                        from forge.build import artwork as art_mod  # noqa: PLC0415
                        problem = art_mod.apply(obj, art["face"], art_path,
                                                art.get("describes", ""))
                        if problem:
                            warnings.append({"id": part["id"],
                                             "why": f"artwork not applied: {problem}"})
                    except Exception as exc:  # noqa: BLE001
                        warnings.append({
                            "id": part["id"],
                            "why": f"artwork failed: {type(exc).__name__}: {exc}"})

            position, rotation = placements.get((part["id"], index), ((0, 0, 0), (0, 0, 0)))
            obj.location = tuple(v * MM for v in position)
            obj.rotation_euler = tuple(math.radians(v) for v in rotation)

            move_to(obj, collection)
            instances.append(obj)

        if instances:
            by_part[part["id"]] = instances
            built.append({"id": part["id"], "objects": [o.name for o in instances]})

    # Parent children to their parent part's first instance, so moving a leg
    # takes its glide with it.
    for part in parts:
        parent_id = part.get("parent_id")
        if not parent_id or parent_id not in by_part or part["id"] not in by_part:
            continue
        anchor = by_part[parent_id][0]
        for child in by_part[part["id"]]:
            child.parent = anchor
            # keep_transform equivalent: preserve world position after parenting
            child.matrix_parent_inverse = anchor.matrix_world.inverted()

    # Order the collections so the outliner reads frame-first rather than in
    # whatever order parts happened to appear.
    scene_children = bpy.context.scene.collection.children
    for name in naming.COLLECTION_ORDER:
        collection = bpy.data.collections.get(name)
        if collection is not None and collection.name in scene_children:
            scene_children.unlink(collection)
            scene_children.link(collection)

    # `checks` describes the assembly that was actually used - it came back
    # from `best_placements` alongside the placements themselves, so the
    # findings and the geometry cannot disagree about which layout they refer
    # to.
    return {"built": built, "skipped": skipped, "warnings": warnings,
            "checks": checks, "strategy": strategy,
            "objects": sum(len(v) for v in by_part.values())}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main():
    argv = sys.argv
    args = argv[argv.index("--") + 1:] if "--" in argv else []

    spec_path = out_dir = None
    for i, token in enumerate(args):
        if token == "--spec":
            spec_path = args[i + 1]
        elif token == "--out":
            out_dir = args[i + 1]

    if not spec_path or not out_dir:
        print("BUILD_ERROR: --spec and --out are required", file=sys.stderr)
        sys.exit(2)

    payload = json.loads(Path(spec_path).read_text())
    spec = payload.get("spec", payload)
    revision = int(payload.get("revision", 0))
    formats = payload.get("formats") or ["blend", "fbx", "obj", "glb", "stl"]
    name = payload.get("name") or "model"

    os.makedirs(out_dir, exist_ok=True)

    result = build(spec, revision, payload.get("artwork") or {})

    from forge.build.preview import render_views  # noqa: PLC0415
    result["previews"] = render_views(out_dir, name)

    from forge.build.export import export_all  # noqa: PLC0415
    result["exports"] = export_all(out_dir, name, formats)

    # The worker reads this line from stdout; Blender writes a great deal of
    # other noise around it, so it is delimited rather than just printed.
    print("FORGE_RESULT_BEGIN")
    print(json.dumps(result))
    print("FORGE_RESULT_END")


if __name__ == "__main__":
    main()
