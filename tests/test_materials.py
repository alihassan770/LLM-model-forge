"""Choosing a colour for a material name.

Three faults lived in this one lookup, and all three had the same shape: a
substring matched something it should not have, or matched something less
specific first. None of them failed anything - every build succeeded and
produced a model that was simply the wrong colour, which no amount of checking
"did it build" will ever catch.

The colour table itself is in `build.py`, which imports `bpy` and so cannot be
imported outside Blender. The resolution logic is what has the bugs in it, so
it is lifted out of the module source and exercised directly. That is uglier
than importing a function, and the alternative is not testing it at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BUILD = Path(__file__).parents[1] / "forge" / "build" / "build.py"


def _tables() -> tuple[dict, frozenset]:
    """MATERIAL_COLOURS and GENERIC_MATERIALS, read without importing bpy."""
    tree = ast.parse(BUILD.read_text())
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in (
                    "MATERIAL_COLOURS", "GENERIC_MATERIALS"):
                value = node.value
                # GENERIC_MATERIALS is `frozenset({...})` - a call, which
                # literal_eval refuses. The set inside it is the literal.
                if isinstance(value, ast.Call) and value.args:
                    value = value.args[0]
                found[target.id] = ast.literal_eval(value)
    return found["MATERIAL_COLOURS"], frozenset(found["GENERIC_MATERIALS"])


COLOURS, GENERIC = _tables()
DEFAULT = (0.55, 0.55, 0.58, 1.0)


def resolve(code: str):
    """The same resolution build.py performs, kept deliberately in step.

    If this drifts from the implementation the tests below stop meaning
    anything, so the structure is copied exactly rather than paraphrased.
    """
    code = (code or "unspecified").strip().lower()
    glass_filled = "glass" in code and ("filled" in code or "fill" in code)

    for keyword in sorted(COLOURS, key=len, reverse=True):
        if keyword in GENERIC:
            continue
        if keyword == "glass" and glass_filled:
            continue
        if keyword in code:
            return COLOURS[keyword]
    for keyword in sorted(GENERIC, key=len, reverse=True):
        if keyword in code:
            return COLOURS[keyword]
    return DEFAULT


def test_the_helper_matches_the_implementation():
    """Guard on the copy above: the real branches must still be in build.py."""
    source = BUILD.read_text()
    assert "if keyword in GENERIC_MATERIALS:" in source
    assert 'if keyword == "glass" and glass_filled:' in source
    assert 'glass_filled = "glass" in code and ("filled" in code' in source


# --------------------------------------------------------------------------- #

def test_a_species_beats_its_family():
    """"hardwood_oak" contains both "wood" and "oak"; oak is what was meant.

    Sorting by length does not settle this - "wood" is longer than "oak" - so
    the family names are held back and only consulted if nothing specific
    matched. Every oak part used to come out generic brown.
    """
    assert resolve("hardwood_oak") == COLOURS["oak"]
    assert resolve("solid_walnut") == COLOURS["walnut"]
    assert resolve("beech_ply") == COLOURS["beech"]


def test_a_metal_beats_the_word_metal():
    """"metal_brass" was grey, because "metal" and "brass" are the same length
    and the tie went to whichever the sort happened to put first."""
    assert resolve("metal_brass") == COLOURS["brass"]
    assert resolve("metal_chrome") == COLOURS["chrome"]


def test_a_family_name_alone_still_resolves():
    assert resolve("wood") == COLOURS["wood"]
    assert resolve("metal") == COLOURS["metal"]


def test_glass_filled_plastic_is_not_glass():
    """Glass-filled nylon and polypropylene are opaque engineering plastics.

    A substring test for "glass" turned a chair's seat pan, back support and
    arms into pale transparent sheets - the parts you could see straight
    through in the preview.
    """
    assert resolve("pp_glassfilled") == COLOURS["pp"]
    assert resolve("nylon_glassfilled") == COLOURS["nylon"]
    assert resolve("glass-filled nylon") == COLOURS["nylon"]


def test_actual_glass_is_still_glass():
    assert resolve("glass") == COLOURS["glass"]
    assert resolve("tempered_glass") == COLOURS["glass"]
    assert resolve("glass")[3] < 1.0, "glass should be the transparent one"


def test_the_materials_a_real_spec_used_are_all_recognised():
    """Every material name from the two fixture specs.

    "pp_glassfilled" was not in the table at all and fell through to the
    default grey, so half an office chair was the colour of nothing in
    particular while the other half was correct.
    """
    import json
    fixtures = Path(__file__).parent / "fixtures"
    seen = set()
    for name in ("office_chair", "sideboard", "table"):
        spec = json.loads((fixtures / f"{name}.json").read_text())
        seen |= {p.get("material", "") for p in spec["parts"] if p.get("material")}

    unrecognised = sorted(code for code in seen if resolve(code) == DEFAULT)
    assert unrecognised == [], f"no colour for: {unrecognised}"


@pytest.mark.parametrize("code", sorted(COLOURS))
def test_every_colour_is_a_valid_rgba(code):
    colour = COLOURS[code]
    assert len(colour) == 4
    assert all(0.0 <= channel <= 1.0 for channel in colour)


# --------------------------------------------------------------------------- #
# A stated dimension must never be silently discarded
# --------------------------------------------------------------------------- #

def _labelled(name, value, unit="cm", source="image_label"):
    return {"name": name, "value": value, "unit": unit,
            "source": source, "confidence": 1.0, "note": ""}


def test_a_seat_keeps_the_width_its_drawing_states():
    """The office chair seat was built 30mm too wide, from a correct reading.

    Its drawing labels the seat "width 43, length 46, thickness 6", and the
    specification carried all three. `box_extent` took `length` for X first,
    so the 430 had nowhere left to go and was dropped - the seat came out
    460 wide and a quarter turn round. Nothing downstream could notice,
    because the one number that contradicted it had been thrown away.
    """
    from forge.spec import dims

    seat = {"primitive": "cushion", "dimensions": [
        _labelled("width", 43), _labelled("length", 46),
        _labelled("thickness", 6),
        _labelled("seat depth (front to back)", 45, source="user"),
    ]}
    x, y, z = dims.box_extent(seat)
    assert x == 430.0          # the stated width, not the length
    assert z == 60.0


def test_a_rail_is_not_reshaped_by_its_own_section_width():
    """`width` on a rail is its section, not a side - the old order is right.

    A 1300mm apron 25mm thick must stay 1300 long. Preferring `width` for X
    whenever it appears would build it 25mm long, which is why the rule only
    applies when a depth is present to pair it with.
    """
    from forge.spec import dims

    apron = {"primitive": "box", "dimensions": [
        _labelled("length", 1300, "mm"), _labelled("width", 25, "mm"),
        _labelled("height", 90, "mm"),
    ]}
    x, _, z = dims.box_extent(apron)
    assert x == 1300.0
    assert z == 90.0


def test_a_cabinet_side_panel_stands_on_its_edge():
    """A side panel came out 414mm wide and 18mm deep - lying across the box.

    Its labels are "height 645, depth 414, thickness 18" and `depth` was being
    offered to X as a third fallback. Depth means front-to-back, which is Y by
    definition, so the panel has to be thin side-to-side and deep front-to-back.
    """
    from forge.spec import dims

    side = {"primitive": "box", "dimensions": [
        _labelled("height", 645, "mm"), _labelled("depth", 414, "mm"),
        _labelled("thickness", 18, "mm"),
    ]}
    x, y, z = dims.box_extent(side)
    assert (x, y, z) == (18.0, 414.0, 645.0)


def test_a_tabletop_is_unaffected_by_the_depth_rule():
    """The common case must not move: length on X, depth on Y, thickness up."""
    from forge.spec import dims

    top = {"primitive": "box", "dimensions": [
        _labelled("length", 1500, "mm"), _labelled("depth", 900, "mm"),
        _labelled("thickness", 30, "mm"),
    ]}
    assert dims.box_extent(top) == (1500.0, 900.0, 30.0)


# --------------------------------------------------------------------------- #
# Round parts: a circle is as wide as it is deep
# --------------------------------------------------------------------------- #

def _round(primitive, **named):
    return {"primitive": primitive, "dimensions": [
        _labelled(k.replace("_", " "), v, "mm") for k, v in named.items()]}


def test_a_round_seat_is_a_disc_not_a_plank_on_edge():
    """A 340mm stool seat was built 5 x 340 x 45.

    The diameter went to one horizontal axis and the other was filled from
    whatever measurement was left over - here a 5mm edge radius. Every round
    part in that stool was wrong the same way: the legs, the glides and the
    mounting plate.
    """
    from forge.spec import dims

    seat = _round("cylinder", diameter=340, thickness=45, edge_radius=5)
    assert dims.box_extent(seat) == (340.0, 340.0, 45.0)


def test_a_round_part_measured_by_height_stands_up():
    from forge.spec import dims

    glide = _round("cylinder", diameter=24, height=20)
    assert dims.box_extent(glide) == (24.0, 24.0, 20.0)


def test_a_round_part_measured_by_length_lies_along_x():
    """The bar handle that used to be a post out of the drawer front.

    A gas lift and a bar handle are both "a long thin cylinder with a length",
    and only the word distinguishes a rail's length from a post's height - so
    `length` means X, which is what a maker means by the length of a rail.
    """
    from forge.spec import dims

    handle = _round("cylinder", length=160, diameter=12)
    assert dims.box_extent(handle) == (160.0, 12.0, 12.0)


def test_the_builder_and_the_bill_agree_which_way_a_round_part_runs():
    """One answer to "which way does this run", shared by both.

    They disagreed once - a leg built 70x70x735 and costed as 800x735x70 - and
    this module exists so that cannot happen again. `round_axis` is what
    `box_extent` and `make_cylinder` both consult.
    """
    from forge.spec import dims

    cases = (
        (_round("cylinder", length=160, diameter=12), "x", 160.0, 12.0),
        (_round("cylinder", diameter=50, height=240), "z", 240.0, 50.0),
        (_round("cylinder", diameter=340, thickness=45), "z", 45.0, 340.0),
    )
    for part, axis, along, across in cases:
        length, got = dims.round_axis(dims.axes(part))
        assert (got, length) == (axis, along)

        # The envelope must put that length on that axis, and the diameter on
        # the other two - which is what the builder then does.
        extent = dims.box_extent(part)
        index = "xyz".index(axis)
        assert extent[index] == along
        assert [v for i, v in enumerate(extent) if i != index] == [across, across]


def test_the_two_extent_functions_never_disagree_about_a_round_part():
    """A bicycle tyre was an envelope 678 x 678 x 28 and a cylinder 678 LONG.

    `cylinder_extent` would not accept `width` as an axial measurement, so a
    wheel measured "diameter 678, width 28" was built as a drum the size of an
    oil barrel. Both functions now read the axis from `round_axis`.
    """
    from forge.spec import dims

    for part in (_round("tube", diameter=678, width=28),
                 _round("cylinder", diameter=50, length=240),
                 _round("cylinder", diameter=340, thickness=45),
                 _round("cylinder", diameter=24, height=20)):
        diameter, length = dims.cylinder_extent(part)
        box = dims.box_extent(part)
        # Whichever axis the part runs down, its envelope is the diameter
        # twice and the axial length once. That is the whole agreement.
        assert sorted(box) == sorted([diameter, diameter, length])
