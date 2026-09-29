"""The placement, appearance and shape a spec now carries.

These cover the arithmetic that turns "one corner, mirrored" into four legs on
the floor. None of it needs Blender: `anchors` is deliberately standard library
only, so the part of the build most likely to be wrong is also the part that
can be tested in a quarter of a second.
"""

from __future__ import annotations

import math

from forge.build import anchors
from forge.build import layout
from forge.build.layout import place
from forge.spec import schema as spec_schema

BOX = {"x_span": 1500.0, "y_span": 900.0, "height": 750.0}


def measurement(name: str, value: float, unit: str = "mm") -> dict:
    return {"name": name, "value": value, "unit": unit,
            "source": "user", "confidence": 1.0, "note": ""}


def part(pid: str, count: int = 1, dimensions=None, **placement) -> dict:
    full = {
        "anchor_x": 0.5, "anchor_y": 0.5, "anchor_z": 0.5,
        "align_x": "center", "align_y": "center", "align_z": "center",
        "run_axis": "", "tilt_x": 0.0, "tilt_y": 0.0, "tilt_z": 0.0,
        "pattern": "single", "spacing_mm": 0.0, "confidence": 0.9,
    }
    full.update(placement)
    return {
        "id": pid, "display_name": pid, "parent_id": "", "role": "structural",
        "primitive": "box", "material": "oak", "count": count, "notes": "",
        "dimensions": dimensions or [
            measurement("width", 70), measurement("depth", 70),
            measurement("height", 720),
        ],
        "placement": full,
    }


# --------------------------------------------------------------------------- #
# Orientation
# --------------------------------------------------------------------------- #

def test_a_part_already_running_the_right_way_is_not_rotated():
    rotation, world = anchors.orient((70.0, 70.0, 720.0), "z")
    assert rotation == (0.0, 0.0, 0.0)
    assert world == (70.0, 70.0, 720.0)


def test_turning_a_rail_across_the_object_swaps_the_axes_it_occupies():
    """A 1200mm rail turned to run front-to-back is 1200 deep, not 1200 wide.

    This is what alignment measures against, so getting the permutation wrong
    insets the part by the wrong dimension - which reads as a rail that does
    not reach its legs.
    """
    rotation, world = anchors.orient((1200.0, 25.0, 90.0), "y")
    assert rotation == (0.0, 0.0, 90.0)
    assert world == (25.0, 1200.0, 90.0)


def test_a_handle_measured_long_is_laid_down_rather_than_stood_up():
    """The 160mm bar handle that used to be built as a post out of the drawer."""
    rotation, world = anchors.orient((160.0, 12.0, 12.0), "x")
    assert rotation == (0.0, 0.0, 0.0)
    assert world[0] == 160.0

    # And asked to stand up, it genuinely does.
    rotation, world = anchors.orient((160.0, 12.0, 12.0), "z")
    assert rotation == (0.0, 90.0, 0.0)
    assert world == (12.0, 12.0, 160.0)


def test_an_unstated_run_axis_leaves_the_part_as_it_was_built():
    rotation, world = anchors.orient((100.0, 50.0, 25.0), "")
    assert rotation == (0.0, 0.0, 0.0)
    assert world == (100.0, 50.0, 25.0)


# --------------------------------------------------------------------------- #
# Alignment - the half-a-thickness errors
# --------------------------------------------------------------------------- #

def test_a_leg_on_the_floor_stands_on_it_rather_than_sinking_into_it():
    leg = part("leg", anchor_z=0.0, align_z="min", run_axis="z")
    poses = anchors.resolve(leg, spec_schema.placement_of(leg),
                            (70.0, 70.0, 720.0), BOX)
    (_, _, z), _ = poses[0]
    assert z == 360.0          # its centre is half its length up


def test_a_top_is_positioned_by_its_upper_face():
    top = part("top", anchor_z=1.0, align_z="max", run_axis="x",
               dimensions=[measurement("length", 1500),
                           measurement("depth", 900),
                           measurement("thickness", 30)])
    poses = anchors.resolve(top, spec_schema.placement_of(top),
                            (1500.0, 900.0, 30.0), BOX)
    (_, _, z), _ = poses[0]
    assert z == 735.0          # 750 minus half of 30: the surface IS at 750


def test_a_centred_part_sits_on_its_anchor_exactly():
    shelf = part("shelf", anchor_z=0.5, align_z="center")
    poses = anchors.resolve(shelf, spec_schema.placement_of(shelf),
                            (800.0, 300.0, 18.0), BOX)
    (_, _, z), _ = poses[0]
    assert z == 375.0


def test_a_part_anchored_to_the_floor_is_never_buried_under_it():
    """`align_z: center` at anchor 0 is a common slip, and a loud one.

    Half a leg underground is far more visible than a leg resting on the
    ground, so the obviously-wrong reading is corrected rather than honoured.
    """
    leg = part("leg", anchor_z=0.0, align_z="center", run_axis="z")
    poses = anchors.resolve(leg, spec_schema.placement_of(leg),
                            (70.0, 70.0, 720.0), BOX)
    (_, _, z), _ = poses[0]
    assert z == 360.0


# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #

def test_four_legs_from_one_corner_land_on_four_corners_symmetrically():
    leg = part("leg", count=4, anchor_x=0.0, anchor_y=0.0, anchor_z=0.0,
               align_x="min", align_y="min", align_z="min",
               run_axis="z", pattern="mirror_xy")
    poses = anchors.resolve(leg, spec_schema.placement_of(leg),
                            (70.0, 70.0, 720.0), BOX)
    assert len(poses) == 4

    corners = sorted((round(p[0][0], 6), round(p[0][1], 6)) for p in poses.values())
    # Inside each edge by half the leg's section, because align said the leg's
    # outer face is on the object's edge.
    assert corners == [(-715.0, -415.0), (-715.0, 415.0),
                       (715.0, -415.0), (715.0, 415.0)]

    # Every leg at the same height, and all four genuinely distinct.
    assert len({round(p[0][2], 6) for p in poses.values()}) == 1
    assert len(set(corners)) == 4


def test_a_mirrored_pair_is_reflected_about_the_centre_line():
    apron = part("apron", count=2, anchor_y=0.1, pattern="mirror_y",
                 run_axis="x")
    poses = anchors.resolve(apron, spec_schema.placement_of(apron),
                            (1300.0, 25.0, 90.0), BOX)
    ys = sorted(round(p[0][1], 6) for p in poses.values())
    assert ys == [-360.0, 360.0]


def test_a_stated_spacing_is_used_verbatim_for_a_row():
    """'Shelves at 320mm centres' is a measurement, not a proportion."""
    shelf = part("shelf", count=3, anchor_z=0.5, pattern="stack_z",
                 spacing_mm=320.0)
    poses = anchors.resolve(shelf, spec_schema.placement_of(shelf),
                            (800.0, 300.0, 18.0), BOX)
    zs = sorted(round(p[0][2], 6) for p in poses.values())
    assert zs == [55.0, 375.0, 695.0]
    assert zs[1] - zs[0] == 320.0


def test_a_row_without_a_spacing_is_spread_across_the_object():
    slat = part("slat", count=5, anchor_x=0.5, pattern="row_x")
    poses = anchors.resolve(slat, spec_schema.placement_of(slat),
                            (30.0, 400.0, 20.0), BOX)
    xs = sorted(round(p[0][0], 6) for p in poses.values())
    assert len(set(xs)) == 5
    gaps = {round(b - a, 6) for a, b in zip(xs, xs[1:])}
    assert len(gaps) == 1                      # evenly spaced
    assert max(abs(x) for x in xs) < BOX["x_span"] / 2   # inside the object


def test_a_star_base_spreads_its_arms_and_turns_each_one_outwards():
    """Five identical arms all pointing the same way is not a star base."""
    arm = part("base.arm", count=5, anchor_x=0.9, anchor_y=0.5,
               anchor_z=0.0, align_z="min", run_axis="x", pattern="radial")
    poses = anchors.resolve(arm, spec_schema.placement_of(arm),
                            (300.0, 40.0, 30.0), BOX)
    assert len(poses) == 5

    angles = sorted(round(p[1][2] % 360.0, 3) for p in poses.values())
    assert len(set(angles)) == 5
    for a, b in zip(angles, angles[1:]):
        assert math.isclose(b - a, 72.0, abs_tol=0.01)

    # All at one radius from the centre, none stacked at the origin.
    radii = {round(math.hypot(p[0][0], p[0][1]), 3) for p in poses.values()}
    assert len(radii) == 1
    assert radii.pop() > 0.0


def test_a_star_base_reaches_the_diameter_the_drawing_states():
    """The office chair's base came out 21% too small.

    Its drawing states a 62cm base and the specification read that correctly.
    The model then anchored the arms sensibly at 0.69 of the width - but an
    anchor names a part's centre, so 270mm arms placed at a 120mm radius
    reached only 255mm of the 310mm they needed, and the base came out 510mm
    across from a specification containing the right number.
    """
    box = {"x_span": 620.0, "y_span": 620.0, "height": 870.0}
    arm = part("base.arm", count=5, anchor_x=0.694, anchor_y=0.5,
               anchor_z=0.11, run_axis="x", pattern="radial",
               dimensions=[measurement("length", 270),
                           measurement("width", 50),
                           measurement("thickness", 40)])
    extent = (270.0, 50.0, 40.0)
    poses = anchors.resolve(arm, spec_schema.placement_of(arm), extent, box)

    tips = [math.hypot(p[0][0], p[0][1]) + 135.0 for p in poses.values()]
    for tip in tips:
        assert math.isclose(tip, 310.0, abs_tol=0.5)   # a 620mm base


def test_a_detail_near_the_hub_is_not_flung_out_to_the_rim():
    """Only the outer ring is corrected; a collar round the column stays put.

    Anchored at 0.58 - 50mm off centre on a 620mm base, well inside the
    quarter-radius bar, and far enough out not to read as "at the middle,
    spread them".
    """
    box = {"x_span": 620.0, "y_span": 620.0, "height": 870.0}
    boss = part("hub.boss", count=4, anchor_x=0.58, anchor_y=0.5,
                anchor_z=0.1, run_axis="x", pattern="radial",
                dimensions=[measurement("length", 30),
                            measurement("width", 20),
                            measurement("thickness", 20)])
    poses = anchors.resolve(boss, spec_schema.placement_of(boss),
                            (30.0, 20.0, 20.0), box)
    radii = {round(math.hypot(p[0][0], p[0][1]), 1) for p in poses.values()}
    assert radii == {49.6}        # left exactly where it was asked to be


def test_a_single_part_is_placed_once_however_the_pattern_reads():
    top = part("top", count=1, pattern="mirror_xy")
    poses = anchors.resolve(top, spec_schema.placement_of(top),
                            (1500.0, 900.0, 30.0), BOX)
    assert list(poses) == [0]


# --------------------------------------------------------------------------- #
# Tilt
# --------------------------------------------------------------------------- #

def test_a_raked_back_keeps_its_lean_on_top_of_the_turn_it_needed():
    back = part("back.post", anchor_z=0.5, run_axis="z", tilt_x=-12.0)
    poses = anchors.resolve(back, spec_schema.placement_of(back),
                            (40.0, 40.0, 500.0), BOX)
    _, rotation = poses[0]
    assert rotation == (-12.0, 0.0, 0.0)


def test_a_lean_composes_with_the_rotation_that_orients_the_part():
    rail = part("rail", run_axis="y", tilt_z=5.0)
    poses = anchors.resolve(rail, spec_schema.placement_of(rail),
                            (1200.0, 25.0, 90.0), BOX)
    _, rotation = poses[0]
    assert rotation == (0.0, 0.0, 95.0)        # 90 to turn it, 5 of lean


# --------------------------------------------------------------------------- #
# Reading the blocks back
# --------------------------------------------------------------------------- #

def test_a_spec_written_before_placement_existed_reports_none():
    """Every stored spec predates this. None of them may start failing."""
    legacy = {"id": "leg", "display_name": "Leg", "count": 4,
              "primitive": "box", "dimensions": []}
    assert spec_schema.placement_of(legacy) is None
    assert not spec_schema.placed_confidently(legacy)


def test_a_position_the_model_doubted_is_not_treated_as_known():
    unsure = part("runner", confidence=0.2)
    assert spec_schema.placement_of(unsure) is not None
    assert not spec_schema.placed_confidently(unsure)


def test_a_nonsense_placement_is_clamped_rather_than_believed():
    wild = part("x", anchor_x=99.0, anchor_z=-40.0, tilt_x=3000.0,
                pattern="sideways", run_axis="q")
    placement = spec_schema.placement_of(wild)
    assert placement["anchor_x"] == 1.5
    assert placement["anchor_z"] == -0.5
    assert placement["tilt_x"] == 90.0
    assert placement["pattern"] == "single"
    assert placement["run_axis"] == ""


def test_colours_are_read_in_both_spellings_and_rejected_when_they_are_not_colours():
    assert spec_schema.parse_hex("#ffffff") == (1.0, 1.0, 1.0)
    assert spec_schema.parse_hex("000") == (0.0, 0.0, 0.0)
    assert spec_schema.parse_hex("#FF0000") == (1.0, 0.0, 0.0)
    for bad in ("", None, "oak", "#12345", "#gggggg", "rgb(1,2,3)"):
        assert spec_schema.parse_hex(bad) is None


def test_an_unreadable_colour_leaves_the_material_table_to_answer():
    look = spec_schema.appearance_of({"appearance": {"hex": "brownish",
                                                     "finish": "matte"}})
    assert look["rgb"] is None
    assert look["finish"] == "matte"


def test_shape_defaults_to_the_plain_solid_it_was_before():
    shape = spec_schema.shape_of({"id": "x"})
    assert shape == {"taper": 1.0, "edge_radius_mm": 0.0, "profile": [],
                     "path": [], "path_plane": "xz", "path_closed": False}


def test_a_profile_of_one_point_is_not_a_profile():
    shape = spec_schema.shape_of({"shape": {"profile": [{"at": 0.5, "scale": 1.0}]}})
    assert shape["profile"] == []


def test_a_profile_is_sorted_and_never_closes_to_nothing():
    shape = spec_schema.shape_of({"shape": {"profile": [
        {"at": 1.0, "scale": 0.3}, {"at": 0.0, "scale": 0.0},
        {"at": 0.4, "scale": 1.2},
    ]}})
    assert [p["at"] for p in shape["profile"]] == [0.0, 0.4, 1.0]
    assert shape["profile"][0]["scale"] > 0.0


# --------------------------------------------------------------------------- #
# The object's own footprint
# --------------------------------------------------------------------------- #

def test_an_internal_width_does_not_get_mistaken_for_the_objects_depth():
    """The bug that exploded the Oakley sideboard.

    Its specification reads the PDF correctly - 1800 wide, 450 deep - and then
    derives `carcass width: 1760`, the inside of the same box. Taking the two
    largest horizontal measurements picks 1800 and 1760, which are two
    measurements of the SAME direction, and the real 450mm depth is never used.

    Every part was then placed against a 1760mm depth instead of a 450mm one,
    so anything positioned by a fraction of the depth landed up to a metre out.
    The parts were all exactly the right size, which is what made it look like
    a placement bug rather than a measurement one.
    """
    spec = {
        "overall": [
            measurement("width", 1800), measurement("depth", 450),
            measurement("height", 820),
            measurement("carcass width", 1760),
            measurement("carcass depth", 414),
            measurement("door width", 876),
        ],
        "parts": [],
    }
    overall = layout._overall(spec)
    assert overall["x_span"] == 1800.0
    assert overall["y_span"] == 450.0


def test_a_maker_who_says_length_and_width_still_gets_the_long_side_on_x():
    """The table case the size-not-name rule was introduced for.

    No measurement here is named `depth`, so the two largest win - which is
    what makes 1520 the long side rather than the 910 a name-based rule would
    have picked.
    """
    spec = {
        "overall": [measurement("length", 1520), measurement("width", 910),
                    measurement("height", 760)],
        "parts": [],
    }
    overall = layout._overall(spec)
    assert overall["x_span"] == 1520.0
    assert overall["y_span"] == 910.0


def test_an_explicit_depth_is_the_short_side_even_when_it_is_the_larger():
    """A deep, narrow cabinet is still depth-on-Y.

    Front-to-back is a direction, not a size ranking, and a part anchored at
    `anchor_y: 0` means the front face whichever number is bigger.
    """
    spec = {
        "overall": [measurement("width", 400), measurement("depth", 900),
                    measurement("height", 2000)],
        "parts": [],
    }
    overall = layout._overall(spec)
    assert overall["x_span"] == 400.0
    assert overall["y_span"] == 900.0


def test_a_spec_naming_only_parts_falls_back_to_the_largest_two():
    """One surviving unqualified name says more about naming than the object."""
    spec = {
        "overall": [measurement("top width", 1200),
                    measurement("top depth", 600),
                    measurement("height", 740)],
        "parts": [],
    }
    overall = layout._overall(spec)
    assert overall["x_span"] == 1200.0
    assert overall["y_span"] == 600.0


# --------------------------------------------------------------------------- #
# Through layout
# --------------------------------------------------------------------------- #

def spec_with(parts: list[dict]) -> dict:
    return {
        "object": {"category": "table"},
        "overall": [measurement("length", 1500), measurement("width", 900),
                    measurement("height", 750)],
        "parts": parts, "joints": [], "missing_dimensions": [],
        "assumptions": [], "readability": {"notes": "", "labels_found": True},
    }


def test_layout_uses_the_models_assembly_when_it_placed_the_object():
    parts = [
        part("top", anchor_z=1.0, align_z="max", run_axis="x",
             dimensions=[measurement("length", 1500), measurement("depth", 900),
                         measurement("thickness", 30)]),
        part("leg", count=4, anchor_x=0.0, anchor_y=0.0, anchor_z=0.0,
             align_x="min", align_y="min", align_z="min",
             run_axis="z", pattern="mirror_xy"),
    ]
    placements = place(spec_with(parts))
    assert len(placements) == 5
    assert placements[("top", 0)][0][2] == 735.0
    assert {round(placements[("leg", i)][0][0], 3) for i in range(4)} == \
        {-715.0, 715.0}


def test_an_unrecognised_part_the_model_placed_goes_where_it_was_seen():
    """The case the row of orphans beside the object was invented for.

    One placed part among unplaced ones is below the trust threshold, so the
    conventions still run - but a name no rule knows is no longer exiled when
    the model could see exactly where it went.
    """
    # A name deliberately outside every convention in layout.py - no "rail",
    # no "apron", no "panel". This is the part that used to end up in the row.
    odd = part("fretwork.pierced", anchor_x=0.5, anchor_y=0.5,
               anchor_z=0.6, align_z="center", run_axis="x", confidence=0.9)
    odd["role"] = "decorative"
    parts = [
        {"id": "top", "display_name": "Top", "parent_id": "", "role": "surface",
         "primitive": "box", "material": "oak", "count": 1, "notes": "",
         "dimensions": [measurement("length", 1500), measurement("depth", 900),
                        measurement("thickness", 30)]},
        {"id": "leg", "display_name": "Leg", "parent_id": "",
         "role": "structural", "primitive": "box", "material": "oak",
         "count": 4, "notes": "",
         "dimensions": [measurement("width", 70), measurement("depth", 70),
                        measurement("height", 720)]},
        odd,
    ]
    placements = place(spec_with(parts))
    (x, y, z), _ = placements[("fretwork.pierced", 0)]
    assert z == 450.0                      # 0.6 of 750, where it was seen
    assert abs(x) < 750.0 and abs(y) < 450.0   # inside the object, not beside it


def test_a_part_the_model_could_not_place_still_goes_beside_the_object():
    """A guessed position inside the object is worse than an honest one outside.

    A part in the wrong place looks like a modelling error and gets trusted; a
    part standing next to the object is visibly unplaced and gets moved.
    """
    hidden = part("runner.internal.hidden", anchor_z=0.5, confidence=0.1)
    parts = [
        {"id": "top", "display_name": "Top", "parent_id": "", "role": "surface",
         "primitive": "box", "material": "oak", "count": 1, "notes": "",
         "dimensions": [measurement("length", 1500), measurement("depth", 900),
                        measurement("thickness", 30)]},
        hidden,
    ]
    placements = place(spec_with(parts))
    (x, y, _), _ = placements[("runner.internal.hidden", 0)]
    assert x > 750.0 or y > 450.0


def test_a_spec_with_no_placement_at_all_still_builds_the_old_way():
    """The whole existing corpus takes this path, and it must not move."""
    parts = [
        {"id": "top", "display_name": "Top", "parent_id": "", "role": "surface",
         "primitive": "box", "material": "oak", "count": 1, "notes": "",
         "dimensions": [measurement("length", 1500), measurement("depth", 900),
                        measurement("thickness", 30)]},
        {"id": "leg", "display_name": "Leg", "parent_id": "",
         "role": "structural", "primitive": "box", "material": "oak",
         "count": 4, "notes": "",
         "dimensions": [measurement("width", 70), measurement("depth", 70),
                        measurement("height", 720)]},
    ]
    placements = place(spec_with(parts))
    assert ("top", 0) in placements
    assert all(("leg", i) in placements for i in range(4))


def test_a_row_anchored_at_an_edge_stays_inside_the_object():
    """Three drawer sides anchored at 0.03 were flung 1.5m off a 1.8m cabinet.

    The anchor is read as the middle of the row, so one naming the first copy
    sends the rest outwards past the end of the object. The row is slid back by
    the least amount that brings it inside, rather than guessing which the
    model meant.
    """
    box = {"x_span": 1800.0, "y_span": 450.0, "height": 820.0}
    sides = part("drawer.side", count=3, anchor_x=0.03, anchor_z=0.87,
                 run_axis="y", pattern="row_x",
                 dimensions=[measurement("length", 380),
                             measurement("width", 12),
                             measurement("height", 150)])
    poses = anchors.resolve(sides, spec_schema.placement_of(sides),
                            (380.0, 12.0, 150.0), box)
    xs = [p[0][0] for p in poses.values()]
    assert min(xs) >= -box["x_span"] / 2 - 1
    assert max(xs) <= box["x_span"] / 2 + 1


def test_sliding_a_row_back_does_not_change_its_spacing():
    """A stated pitch is a measurement; only the vague position is corrected."""
    box = {"x_span": 1800.0, "y_span": 450.0, "height": 820.0}
    sides = part("drawer.front", count=3, anchor_x=0.02, anchor_z=0.8,
                 run_axis="x", pattern="row_x", spacing_mm=560.0,
                 dimensions=[measurement("length", 540),
                             measurement("height", 180),
                             measurement("thickness", 18)])
    poses = anchors.resolve(sides, spec_schema.placement_of(sides),
                            (540.0, 18.0, 180.0), box)
    xs = sorted(p[0][0] for p in poses.values())
    gaps = [round(b - a, 3) for a, b in zip(xs, xs[1:])]
    assert gaps == [560.0, 560.0]
    assert min(xs) >= -box["x_span"] / 2 - 1


def test_a_row_longer_than_the_object_is_centred_rather_than_shoved():
    box = {"x_span": 600.0, "y_span": 400.0, "height": 700.0}
    slats = part("slat", count=4, anchor_x=0.9, pattern="row_x",
                 spacing_mm=400.0,
                 dimensions=[measurement("length", 20),
                             measurement("width", 300),
                             measurement("height", 20)])
    poses = anchors.resolve(slats, spec_schema.placement_of(slats),
                            (20.0, 300.0, 20.0), box)
    xs = sorted(p[0][0] for p in poses.values())
    assert round((xs[0] + xs[-1]) / 2, 6) == 0.0     # centred on the object


def test_a_round_object_gets_its_footprint_from_its_widest_diameter():
    """A bar stool was assembled inside a 1200 x 700 dining table.

    Every horizontal measurement a stool states is a diameter, and the span
    filter drops those - correctly, since a leg's diameter is not a table's
    footprint - so nothing was left and the default footprint applied. Its
    legs ended up most of a metre apart.
    """
    spec = {
        "overall": [measurement("height", 760), measurement("seat diameter", 340),
                    measurement("foot ring diameter", 300),
                    measurement("leg diameter", 28)],
        "parts": [],
    }
    overall = layout._overall(spec)
    assert overall["x_span"] == 340.0
    assert overall["y_span"] == 340.0


def test_a_rectangular_object_ignores_the_round_fallback():
    """A table states a width and a depth, so diameters stay irrelevant."""
    spec = {
        "overall": [measurement("length", 1520), measurement("width", 910),
                    measurement("height", 760), measurement("leg diameter", 60)],
        "parts": [],
    }
    overall = layout._overall(spec)
    assert (overall["x_span"], overall["y_span"]) == (1520.0, 910.0)


# --------------------------------------------------------------------------- #
# Which way a panel faces
# --------------------------------------------------------------------------- #

def test_a_panel_is_turned_by_the_face_it_is_flush_against():
    """A phone's front glass came out 0.85mm wide and 77.6mm deep.

    `run_axis` says where the longest side goes and leaves the other two
    interchangeable - and for a flat part the difference between them is the
    whole shape. The glass is 163 x 77.6 x 0.85; told only that the 163 stands
    up, the remaining quarter turn was arbitrary and the phone came out
    standing on edge with its screen facing sideways.

    `align_y: "min"` already said the glass is flush front-to-back. Being flush
    is a statement about the thin direction, so it settles the turn.
    """
    _, world = anchors.orient((163.0, 77.6, 0.85), "z", "y")
    assert [round(v, 2) for v in world] == [77.6, 0.85, 163.0]


def test_the_same_panel_flush_sideways_faces_the_other_way():
    _, world = anchors.orient((163.0, 77.6, 0.85), "z", "x")
    assert [round(v, 2) for v in world] == [0.85, 77.6, 163.0]


def test_a_flush_face_is_read_from_the_alignment():
    front = part("glass.front", align_y="min", run_axis="z")
    assert anchors.flat_axis(spec_schema.placement_of(front)) == "y"

    centred = part("frame.band", run_axis="z")
    assert anchors.flat_axis(spec_schema.placement_of(centred)) == ""

    # Two flush faces describe a part wedged into a corner, which says nothing
    # about which way it is thin.
    corner = part("filler", align_x="min", align_y="max", run_axis="z")
    assert anchors.flat_axis(spec_schema.placement_of(corner)) == ""

    # The run axis cannot also be the thin axis.
    shelf = part("shelf", align_z="min", run_axis="x")
    assert anchors.flat_axis(spec_schema.placement_of(shelf)) == ""


def test_an_unaligned_part_is_left_exactly_as_it_was_built():
    """No alignment, no reorientation - not even a sensible-sounding one.

    Assuming an object is wider than it is deep was tried here, because it is
    true of most things that stand on a floor. It fixed a phone's chassis rail
    and laid every cabinet side panel flat across its carcass: a side panel is
    deliberately thin side-to-side, and the rule read its 414mm depth as a
    width. The sideboard went from passing to 20% too wide.

    A guess applied where the specification said nothing is the confidently
    wrong geometry this module exists to avoid.
    """
    _, world = anchors.orient((163.0, 77.6, 8.25), "z", "")
    assert [round(v, 2) for v in world] == [8.25, 77.6, 163.0]


def test_a_cabinet_side_panel_keeps_its_thin_face_side_to_side():
    """The case that withdrew the rule above: 18mm across, 414mm deep."""
    _, world = anchors.orient((18.0, 414.0, 645.0), "z")
    assert world == (18.0, 414.0, 645.0)


def test_the_wider_than_deep_rule_never_moves_a_height_off_z():
    """An apron turned front-to-back keeps its 90mm height standing up.

    Up is not interchangeable with along, so the convention only ever chooses
    between X and Y. Reading this rail's height as a width would lay the board
    flat.
    """
    rotation, world = anchors.orient((1200.0, 25.0, 90.0), "y")
    assert world == (25.0, 1200.0, 90.0)
    assert rotation == (0.0, 0.0, 90.0)


def test_two_similar_sides_stay_where_they_were():
    """A square-section handle has no wider side, so nothing is reordered."""
    rotation, world = anchors.orient((160.0, 12.0, 12.0), "z")
    assert world == (12.0, 12.0, 160.0)
    assert rotation == (0.0, 90.0, 0.0)      # a simple swap, not a 3-cycle


def test_a_cube_is_never_rotated():
    assert anchors.orient((50.0, 50.0, 50.0), "x", "y") == (
        (0.0, 0.0, 0.0), (50.0, 50.0, 50.0))


def test_a_wheel_told_its_axle_stands_up_rather_than_lying_flat():
    """A 678mm bicycle wheel was laid flat on the ground like a dinner plate.

    Two of a disc's sides are the same diameter, so "which way does the longest
    side point" has no answer - naming one of those axes says nothing at all.
    The one axis that IS distinguishable is the one through the middle, and
    that is invariably what was meant.
    """
    _, world = anchors.orient((678.0, 678.0, 28.0), "y")
    assert world == (678.0, 28.0, 678.0)


def test_a_round_base_told_its_axis_lies_flat_on_the_floor():
    _, world = anchors.orient((210.0, 210.0, 26.0), "z")
    assert world == (210.0, 210.0, 26.0)


def test_a_long_part_is_not_treated_as_a_disc():
    """The rule must not touch anything with a genuine long axis."""
    _, leg = anchors.orient((70.0, 70.0, 720.0), "z")
    assert leg == (70.0, 70.0, 720.0)

    _, handle = anchors.orient((160.0, 12.0, 12.0), "z")
    assert handle == (12.0, 12.0, 160.0)


def test_an_explicit_flush_face_still_wins_on_a_disc():
    """A stated alignment is evidence; the disc rule is an inference."""
    _, world = anchors.orient((678.0, 678.0, 28.0), "y", "z")
    assert world == (678.0, 678.0, 28.0)


def test_a_plan_can_be_swapped_for_an_object_long_front_to_back():
    """`swap_plan` puts the length on Y, for anything you sit in line with.

    A table's long axis is side-to-side and a bicycle's is front-to-back, and
    no measurement tells them apart. The argument exists and is correct; it is
    not currently used to choose an assembly, because on every object tested
    the swapped reading measured worse - a bike's trouble is not which way
    round it is, but that a diamond frame cannot be described by anchors.
    """
    spec = {
        "object": {"category": "bicycle"},
        "overall": [measurement("length", 1700), measurement("width", 440),
                    measurement("height", 1010)],
        "parts": [part("wheel", count=2, anchor_y=0.2, anchor_z=0.0,
                       align_z="min", run_axis="y", pattern="mirror_y",
                       dimensions=[measurement("diameter", 678),
                                   measurement("width", 28)])],
        "joints": [], "missing_dimensions": [], "assumptions": [],
        "readability": {"notes": "", "labels_found": True},
    }
    normal = layout.place(spec)
    swapped = layout.place(spec, swap_plan=True)

    spread = lambda p: abs(p[("wheel", 0)][0][1] - p[("wheel", 1)][0][1])
    assert spread(normal) < 300      # squashed into the 440mm width
    assert spread(swapped) > 900     # about a wheelbase apart


# --------------------------------------------------------------------------- #
# Which axis a ring of parts turns about
# --------------------------------------------------------------------------- #

def _ring(pattern: str, count: int = 16) -> dict:
    return part("wheel.spoke", count=count, anchor_x=0.5, anchor_y=0.2,
                anchor_z=0.34, run_axis="z", pattern=pattern,
                dimensions=[measurement("diameter", 2),
                            measurement("length", 270)])


BIKE = {"x_span": 440.0, "y_span": 1713.0, "height": 1010.0}


def test_a_wheels_spokes_stand_in_the_wheels_own_plane():
    """64 spokes spun about the vertical laid flat in a horizontal ring.

    A bicycle grew a curtain of sticks through its middle, 1037mm wide where
    the drawing says 440. A wheel turns about its axle, not about the vertical.
    """
    spoke = _ring("radial_x")
    poses = anchors.resolve(spoke, spec_schema.placement_of(spoke),
                            (2.0, 2.0, 270.0), BIKE)
    xs = [p[0][0] for p in poses.values()]
    ys = [p[0][1] for p in poses.values()]
    zs = [p[0][2] for p in poses.values()]
    assert max(xs) - min(xs) < 1.0          # flat across the axle
    # A ring one spoke across, centred on the hub - NOT on the whole bicycle.
    # Measuring the radius from the object's centre put these 514mm out, in a
    # cone around the entire frame.
    assert 260 < max(ys) - min(ys) < 280
    assert 260 < max(zs) - min(zs) < 280


def test_a_wheel_turns_about_its_own_hub_not_the_objects_centre():
    """A wheel is not at the middle of a bicycle.

    `_radial` assumed the ring's centre and the object's centre are the same
    place, which holds for a star base or a pedestal. The anchor's distance
    from a bike's centre is half a wheelbase, and read as a radius it scattered
    the spokes around the whole frame.
    """
    spoke = _ring("radial_x", count=8)
    poses = anchors.resolve(spoke, spec_schema.placement_of(spoke),
                            (2.0, 2.0, 270.0), BIKE)
    hub_y = (0.2 - 0.5) * BIKE["y_span"]
    centres_y = [p[0][1] for p in poses.values()]
    assert abs(sum(centres_y) / len(centres_y) - hub_y) < 1.0


def test_each_spoke_is_turned_about_the_axle():
    spoke = _ring("radial_x", count=8)
    poses = anchors.resolve(spoke, spec_schema.placement_of(spoke),
                            (2.0, 2.0, 270.0), BIKE)
    tilts = sorted(round(p[1][0] % 360.0, 1) for p in poses.values())
    assert len(set(tilts)) == 8
    for a, b in zip(tilts, tilts[1:]):
        assert math.isclose(b - a, 45.0, abs_tol=0.1)


def test_a_chair_base_still_spins_about_the_vertical():
    """The default must not move: a five-star base lies on the floor."""
    arm = part("base.arm", count=5, anchor_x=0.9, anchor_y=0.5, anchor_z=0.1,
               run_axis="x", pattern="radial")
    poses = anchors.resolve(arm, spec_schema.placement_of(arm),
                            (300.0, 40.0, 30.0),
                            {"x_span": 620.0, "y_span": 620.0, "height": 870.0})
    zs = {round(p[0][2], 3) for p in poses.values()}
    assert len(zs) == 1                     # all at one height, flat on the floor


def test_the_spin_axis_comes_from_the_pattern_name():
    for pattern, axis in (("radial", "z"), ("radial_x", "x"),
                          ("radial_y", "y")):
        placement = spec_schema.placement_of(part("p", pattern=pattern))
        assert placement["spin_axis"] == axis
        assert placement["pattern"] == pattern


def test_an_old_spec_without_a_spin_axis_still_spins_about_z():
    placement = spec_schema.placement_of(part("p", pattern="single"))
    assert placement["spin_axis"] == "z"


def test_an_anchor_that_cannot_be_a_row_centre_is_read_as_the_first_copy():
    """A display's four shelves anchored at 0.145 with a 254mm pitch.

    Read as the middle of the row they run from -0.07 to 0.36 and the bottom
    shelf is 126mm underground. Read as the first they run 254, 508, 762 and
    1016mm up the carcass, which is where the drawing puts them - above the
    221mm plinth.

    The middle is still tried first, because it is the commoner meaning and
    what `stack_z` implies. Only a row that will not fit is re-read.
    """
    box = {"x_span": 914.0, "y_span": 381.0, "height": 1754.0}
    shelf = part("shelf.deck", count=4, anchor_z=0.145, align_z="max",
                 run_axis="x", pattern="stack_z", spacing_mm=254.0,
                 dimensions=[measurement("width", 864),
                             measurement("depth", 321),
                             measurement("thickness", 25)])
    poses = anchors.resolve(shelf, spec_schema.placement_of(shelf),
                            (864.0, 321.0, 25.0), box)
    tops = sorted(round(p[0][2] + 12.5) for p in poses.values())
    assert tops == [254, 508, 762, 1016]


def test_a_row_that_fits_as_a_centre_is_left_alone():
    """The commoner meaning must keep working."""
    box = {"x_span": 800.0, "y_span": 300.0, "height": 2000.0}
    shelf = part("shelf", count=3, anchor_z=0.5, pattern="stack_z",
                 spacing_mm=320.0)
    poses = anchors.resolve(shelf, spec_schema.placement_of(shelf),
                            (700.0, 280.0, 18.0), box)
    zs = sorted(round(p[0][2]) for p in poses.values())
    assert zs == [680, 1000, 1320]      # centred on 0.5 of 2000


def test_a_horizontal_panel_keeps_its_thin_face_upward():
    """A display shelf was stood on edge, 321mm tall and 160mm underground.

    `align_y: "min"` on a shelf means its front edge is level with the
    carcass - where it sits, not which way it is thin. Only an upright part,
    whose long axis is vertical, has a genuine ambiguity to resolve.
    """
    shelf = part("shelf.deck", align_y="min", run_axis="x")
    assert anchors.flat_axis(spec_schema.placement_of(shelf)) == ""

    glass = part("glass.front", align_y="min", run_axis="z")
    assert anchors.flat_axis(spec_schema.placement_of(glass)) == "y"

    _, world = anchors.orient((864.0, 321.0, 25.0), "x", "")
    assert world == (864.0, 321.0, 25.0)
