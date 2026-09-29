"""Bent parts: the curve, and what it costs.

A swept member is the first primitive whose material is not its bounding box.
An armrest loop 260 x 190 is bent from about 700mm of tube and occupies roughly
a fifth of the volume of the box it sits in, so the cutting list and the model
have to agree about which of those two numbers is the part - exactly the sort
of disagreement `forge/spec/dims.py` exists to prevent.
"""

from __future__ import annotations

import math

from forge.spec import dims, schema


def measurement(name: str, value: float, unit: str = "mm") -> dict:
    return {"name": name, "value": value, "unit": unit,
            "source": "user", "confidence": 1.0, "note": ""}


# A closed armrest hoop drawn in the side elevation.
LOOP = [{"u": 0.08, "v": 0.0}, {"u": 0.0, "v": 0.55}, {"u": 0.10, "v": 0.95},
        {"u": 0.62, "v": 1.0}, {"u": 0.95, "v": 0.86}, {"u": 0.88, "v": 0.55},
        {"u": 0.70, "v": 0.30}, {"u": 0.40, "v": 0.08}]


def arm_loop(closed: bool = True) -> dict:
    return {
        "id": "arm.loop", "display_name": "Armrest loop", "parent_id": "",
        "role": "structural", "primitive": "sweep", "material": "steel_tube",
        "count": 2, "notes": "",
        "dimensions": [measurement("depth", 260), measurement("height", 190),
                       measurement("diameter", 22)],
        "shape": {"taper": 1.0, "edge_radius_mm": 0.0, "profile": [],
                  "path": LOOP, "path_plane": "yz", "path_closed": closed},
    }


# --------------------------------------------------------------------------- #
# Reading a path back
# --------------------------------------------------------------------------- #

def test_a_path_keeps_the_order_it_was_drawn_in():
    """A turned profile is sorted; a path must never be.

    The order of a path IS the curve. Sorting an armrest hoop by one
    coordinate would untie it into a zigzag.
    """
    shape = schema.shape_of(arm_loop())
    assert [round(p["u"], 3) for p in shape["path"]] == \
        [round(p["u"], 3) for p in LOOP]
    assert shape["path_closed"] is True
    assert shape["path_plane"] == "yz"


def test_two_points_are_a_straight_line_and_not_a_sweep():
    part = arm_loop()
    part["shape"]["path"] = [{"u": 0.0, "v": 0.0}, {"u": 1.0, "v": 1.0}]
    assert schema.shape_of(part)["path"] == []


def test_an_unknown_plane_falls_back_to_the_front_elevation():
    part = arm_loop()
    part["shape"]["path_plane"] = "diagonal"
    assert schema.shape_of(part)["path_plane"] == "xz"


def test_a_part_with_no_shape_block_has_no_path():
    shape = schema.shape_of({"id": "leg", "primitive": "box"})
    assert shape["path"] == []
    assert shape["path_closed"] is False


def test_sweep_is_a_primitive_the_schema_accepts():
    assert "sweep" in schema.PRIMITIVES


# --------------------------------------------------------------------------- #
# What a bent part is made of
# --------------------------------------------------------------------------- #

def test_a_loop_is_costed_by_its_curve_not_by_its_box():
    """260 x 190 of envelope, but roughly 700mm of tube."""
    part = arm_loop()
    length = dims.path_length_mm(part)
    assert 600 < length < 850, f"a 260x190 hoop is about 700mm of tube, got {length}"

    # And that is the length it would be cut to.
    assert dims.longest_mm(part) == length


def test_a_closed_path_includes_the_segment_that_closes_it():
    closed = dims.path_length_mm(arm_loop(closed=True))
    open_ = dims.path_length_mm(arm_loop(closed=False))
    assert closed > open_


def test_a_bent_tube_uses_far_less_material_than_its_bounding_box():
    """The number that matters to whoever buys the steel."""
    part = arm_loop()
    volume = dims.volume_mm3(part)
    x, y, z = dims.box_extent(part)
    assert volume < x * y * z * 0.3

    # Section area times path length, within rounding.
    expected = math.pi * 11.0 ** 2 * dims.path_length_mm(part)
    assert math.isclose(volume, expected, rel_tol=0.01)


def test_a_flat_bent_member_is_costed_on_its_rectangular_section():
    part = arm_loop()
    part["dimensions"] = [measurement("depth", 260), measurement("height", 190),
                          measurement("thickness", 12)]
    width, thickness, round_section = dims.sweep_section(part)
    assert round_section is False
    volume = dims.volume_mm3(part)
    assert math.isclose(volume, width * thickness * dims.path_length_mm(part),
                        rel_tol=0.01)


def test_a_sweep_with_no_path_is_costed_as_the_box_it_gets_built_as():
    """The builder falls back to a box, so the bill must agree with it."""
    part = arm_loop()
    part["shape"]["path"] = []
    x, y, z = dims.box_extent(part)
    assert math.isclose(dims.volume_mm3(part), x * y * z, rel_tol=1e-6)


def test_the_surface_of_a_loop_is_its_perimeter_along_its_length():
    part = arm_loop()
    area = dims.surface_mm2(part)
    expected = math.pi * 22.0 * dims.path_length_mm(part)
    assert math.isclose(area, expected, rel_tol=0.01)


# --------------------------------------------------------------------------- #
# It still has to fit where it is put
# --------------------------------------------------------------------------- #

def test_a_swept_part_occupies_the_envelope_it_was_measured_as():
    """The shape comes from the drawing; the size comes from the measurements.

    A path is normalised into the part's own bounding box, so however the
    curve is drawn the part still fills exactly the space the specification
    states - which is what keeps placement and the audit's envelope check
    meaningful for bent parts.
    """
    x, y, z = dims.box_extent(arm_loop())
    assert (y, z) == (260.0, 190.0)


def test_a_sweep_needs_three_dimensions_before_it_has_a_shape():
    from forge.build import audit

    assert audit.REQUIRED_DIMENSIONS["sweep"] == 3
