"""Where the parts of a cabinet end up.

Every fault these tests describe was a real one, and every one of them was
invisible from the outside: the build reported "63 objects, no failures" while
the handles stood on end, the shelf pins floated in mid-air spaced out as if
they were shelves, and a drawer back sat 21mm outside a cabinet 450mm deep.
Nothing in the pipeline could tell, because a part in the wrong place is a
perfectly valid mesh.

So the assertions are about *relationships between parts* - the shelf is
between the rows, the pins are under the shelf, the hinges are on the edge the
handles are not - rather than about coordinates. A coordinate test has to be
rewritten whenever a dimension changes and tells you nothing when it fails. A
relationship is what somebody actually means by "the cabinet is right", and it
holds for any cabinet.

No database and no Blender: `place()` is pure arithmetic over the spec, which
is the whole reason the geometry decisions live there rather than in the build
script.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge.build import carcass
from forge.build.layout import place
from forge.spec import dims

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(scope="module")
def sideboard() -> dict:
    return load("sideboard")


@pytest.fixture(scope="module")
def laid_out(sideboard) -> dict:
    return place(sideboard)


def extent_of(spec: dict, part_id: str) -> tuple[float, float, float]:
    return dims.box_extent(next(p for p in spec["parts"] if p["id"] == part_id))


def pos(laid_out: dict, part_id: str, index: int = 0):
    return laid_out[(part_id, index)][0]


def rot(laid_out: dict, part_id: str, index: int = 0):
    return laid_out[(part_id, index)][1]


# --------------------------------------------------------------------------- #
# Which rules apply
# --------------------------------------------------------------------------- #

def test_a_sideboard_is_recognised_as_a_carcass(sideboard):
    assert carcass.looks_like_a_carcass(sideboard["parts"])


def test_a_table_is_not(  ):
    """A table must keep the frame rules.

    The carcass rules would be actively wrong for it: they put the bottom panel
    at the top of the legs and hang fronts in an opening a table does not have.
    One signal is not enough to switch - a desk has a modesty panel and is
    still a frame.
    """
    assert not carcass.looks_like_a_carcass(load("table")["parts"])


def test_a_table_still_places_every_part():
    table = load("table")
    placed = place(table)
    assert len(placed) == sum(p.get("count", 1) for p in table["parts"])


# --------------------------------------------------------------------------- #
# The box itself
# --------------------------------------------------------------------------- #

def test_the_carcass_stands_on_the_legs(sideboard, laid_out):
    _, _, leg_height = extent_of(sideboard, "leg")
    _, _, side_height = extent_of(sideboard, "carcass.side")
    assert pos(laid_out, "carcass.side")[2] == pytest.approx(
        leg_height + side_height / 2, abs=1.0)


def test_the_top_caps_the_carcass(sideboard, laid_out):
    _, _, leg_height = extent_of(sideboard, "leg")
    _, _, side_height = extent_of(sideboard, "carcass.side")
    _, _, top_thickness = extent_of(sideboard, "top")
    assert pos(laid_out, "top")[2] == pytest.approx(
        leg_height + side_height + top_thickness / 2, abs=1.0)


def test_the_sides_are_at_the_outer_edges(sideboard, laid_out):
    width = 1800.0
    _, _, _ = extent_of(sideboard, "carcass.side")
    wall = 18.0
    assert abs(pos(laid_out, "carcass.side")[0]) == pytest.approx(
        width / 2 - wall / 2, abs=1.0)


def test_the_back_closes_the_rear_and_the_fronts_face_out(laid_out):
    assert pos(laid_out, "carcass.back")[1] > 0
    assert pos(laid_out, "door")[1] < 0
    assert pos(laid_out, "drawer.front")[1] < 0


# --------------------------------------------------------------------------- #
# The front
# --------------------------------------------------------------------------- #

def test_the_drawers_sit_above_the_doors(sideboard, laid_out):
    _, _, door_h = extent_of(sideboard, "door")
    _, _, front_h = extent_of(sideboard, "drawer.front")
    door_top = pos(laid_out, "door")[2] + door_h / 2
    drawer_bottom = pos(laid_out, "drawer.front")[2] - front_h / 2
    assert drawer_bottom >= door_top - 1.0


def test_the_fixed_shelf_takes_its_own_thickness_between_the_rows(
        sideboard, laid_out):
    """It divides the rows, so it occupies space rather than sharing it.

    Placed at the boundary without advancing the stack past it, the shelf
    overlapped the row above by 18mm: two solid objects in the same place.
    """
    _, _, door_h = extent_of(sideboard, "door")
    _, _, front_h = extent_of(sideboard, "drawer.front")
    _, _, shelf_t = extent_of(sideboard, "shelf.drawer")
    door_top = pos(laid_out, "door")[2] + door_h / 2
    drawer_bottom = pos(laid_out, "drawer.front")[2] - front_h / 2
    shelf = pos(laid_out, "shelf.drawer")[2]
    assert shelf - shelf_t / 2 >= door_top - 0.1
    assert shelf + shelf_t / 2 <= drawer_bottom + 0.1


def test_three_drawers_spread_across_the_width(laid_out):
    xs = {round(pos(laid_out, "drawer.front", i)[0]) for i in range(3)}
    assert len(xs) == 3


def test_two_doors_sit_either_side_of_centre(laid_out):
    assert pos(laid_out, "door", 0)[0] < 0 < pos(laid_out, "door", 1)[0]


def test_dividers_stand_between_the_drawer_bays(laid_out):
    """Three bays across a 1764mm opening have their dividers at +/-294."""
    assert abs(pos(laid_out, "divider.drawer")[0]) == pytest.approx(294, abs=2)


# --------------------------------------------------------------------------- #
# Hardware
# --------------------------------------------------------------------------- #

def test_a_drawer_handle_lies_across_its_front(laid_out):
    assert rot(laid_out, "handle.drawer") == (0.0, 0.0, 0.0)


def test_a_door_handle_stands_upright(laid_out):
    assert rot(laid_out, "handle.door") == (0.0, 90.0, 0.0)


def test_handles_and_hinges_take_opposite_edges(laid_out):
    """A pair of doors meets in the middle: handles there, hinges outside.

    Both on the same edge is a door that cannot open, and it looked entirely
    plausible in a render.
    """
    assert abs(pos(laid_out, "handle.door")[0]) < 300
    assert abs(pos(laid_out, "hinge")[0]) > 800


def test_handles_stand_proud_of_the_front_they_serve(laid_out):
    assert pos(laid_out, "handle.drawer")[1] < pos(laid_out, "drawer.front")[1]


def test_shelf_pins_carry_the_shelf_they_belong_to(sideboard, laid_out):
    """Under the shelf and against the panel, not distributed as shelves.

    The pin loop used to work out its heights by scanning everything already
    placed whose name contained "shelf". "pin.shelf" contains "shelf", and the
    shelf-spacing loop had just laid the eight pins out as eight evenly-spaced
    shelves - so the pins took their heights from their own wrong placement and
    agreed with themselves. They came to rest at 214mm and 261mm, supporting
    the shelves at 307mm and 446mm from thin air.
    """
    _, _, shelf_t = extent_of(sideboard, "shelf.adjustable")
    _, _, pin_h = extent_of(sideboard, "pin.shelf")
    pin_len, _, _ = extent_of(sideboard, "pin.shelf")
    shelf_z = pos(laid_out, "shelf.adjustable")[2]

    assert pos(laid_out, "pin.shelf")[2] == pytest.approx(
        shelf_z - shelf_t / 2 - pin_h / 2, abs=0.5)
    assert abs(pos(laid_out, "pin.shelf")[0]) == pytest.approx(
        1764 / 2 - pin_len / 2, abs=1.0)


# --------------------------------------------------------------------------- #
# Nothing left over, nothing outside
# --------------------------------------------------------------------------- #

def test_a_drawer_box_is_as_deep_as_its_sides(laid_out):
    """The back is placed from the box's depth, not from its own width.

    Using the back panel's width put it 246mm from centre in a cabinet 450mm
    deep, so it hung 21mm out of the back of the finished model.
    """
    assert abs(pos(laid_out, "drawer.back")[1]) <= 225.0


def test_no_part_escapes_the_stated_envelope(laid_out):
    """1800 x 450 x 820, as the specification says."""
    outside = [
        key for key, (position, _) in laid_out.items()
        if abs(position[0]) > 950 or abs(position[1]) > 240
        or not -5 <= position[2] <= 830
    ]
    assert outside == []


def test_every_instance_of_every_part_is_placed(sideboard, laid_out):
    assert len(laid_out) == sum(p.get("count", 1) for p in sideboard["parts"])


# --------------------------------------------------------------------------- #
# The resolver the builder and the bill of materials share
# --------------------------------------------------------------------------- #

def test_the_builder_has_no_dimension_resolver_of_its_own():
    """One answer to "how big is this part", or the bill contradicts the model.

    build.py aliased these to dims.py and then redefined them a few lines
    below, so the alias never took effect and the copies drifted. This asserts
    the names in build.py *are* the ones from dims.py, which a second copy
    cannot satisfy.
    """
    source = (Path(__file__).parents[1] / "forge" / "build" / "build.py").read_text()
    assert "def box_extent" not in source
    assert "def dims_mm" not in source
    assert "box_extent = spec_dims.box_extent" in source


def test_a_repeated_measurement_does_not_become_an_axis(sideboard):
    """A handle given its length twice is still 12mm thick.

    The spec states "length 160", "diameter 12" and "handle length and fixing
    centres 160". Reading the third as an unknown extra made it the height, and
    the handle was built as a 160mm post standing out of the drawer front.
    """
    assert extent_of(sideboard, "handle.drawer") == extent_of(
        sideboard, "handle.door")
    assert extent_of(sideboard, "handle.drawer") == (160.0, 12.0, 12.0)
