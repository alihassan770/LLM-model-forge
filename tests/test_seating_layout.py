"""Where the parts of a chair end up.

The office chair in the fixture was extracted correctly - every label on the
drawing was read, with notes and confidences - and then assembled into a
diagonal line of 27 parts, because the layout knew about frames and boxes and
nothing else. The lesson is that a good specification is not enough on its own:
"which way up does this go" is a question about furniture, not about the model
that read the picture, and it has to be answered in code.

As in the carcass tests, the assertions are about relationships - the back is
behind the seat, the castors are on the floor, the arms are above the seat and
outside it - because those are what "the chair is right" actually means, and
they stay true when the dimensions change.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from forge.build import carcass, seating
from forge.build.layout import _named, place
from forge.spec import dims

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(scope="module")
def spec() -> dict:
    return load("office_chair")


@pytest.fixture(scope="module")
def laid_out(spec) -> dict:
    return place(spec)


@pytest.fixture(scope="module")
def chair(spec) -> seating.Chair:
    return seating.measure(spec["parts"], _named(spec), None, dims.box_extent)


def extent_of(spec: dict, part_id: str):
    return dims.box_extent(next(p for p in spec["parts"] if p["id"] == part_id))


def pos(laid_out, part_id, index=0):
    return laid_out[(part_id, index)][0]


# --------------------------------------------------------------------------- #
# Which rules apply
# --------------------------------------------------------------------------- #

def test_an_office_chair_is_recognised_as_seating(spec):
    assert seating.looks_like_seating(spec["parts"])


def test_a_chair_is_not_mistaken_for_a_carcass(spec):
    """Order matters in the dispatch; only one set of rules may apply."""
    assert not carcass.looks_like_a_carcass(spec["parts"])


def test_a_sideboard_is_not_mistaken_for_seating():
    assert not seating.looks_like_seating(load("sideboard")["parts"])


def test_a_table_is_not_mistaken_for_seating():
    assert not seating.looks_like_seating(load("table")["parts"])


def test_a_pedestal_base_is_detected(chair):
    assert chair.pedestal


# --------------------------------------------------------------------------- #
# Reading the drawing
# --------------------------------------------------------------------------- #

def test_the_seat_height_comes_from_the_drawing(chair):
    """The drawing says 41-51cm. The bottom of the travel is the stated fact."""
    assert chair.seat_top == pytest.approx(410.0)


def test_the_base_diameter_comes_from_the_drawing(chair):
    """62cm castor to castor, so 310mm from the centre to each castor."""
    assert chair.base_radius == pytest.approx(310.0)


def test_the_arm_height_comes_from_the_drawing(chair):
    assert chair.arm_height == pytest.approx(190.0)


# --------------------------------------------------------------------------- #
# The seat and what is attached to it
# --------------------------------------------------------------------------- #

def test_the_seat_surface_is_at_the_stated_height(spec, laid_out):
    _, _, thickness = extent_of(spec, "seat.cushion")
    assert pos(laid_out, "seat.cushion")[2] + thickness / 2 == pytest.approx(410.0)


def test_the_deck_is_directly_under_the_cushion(spec, laid_out):
    _, _, cushion = extent_of(spec, "seat.cushion")
    _, _, deck = extent_of(spec, "seat.deck")
    cushion_bottom = pos(laid_out, "seat.cushion")[2] - cushion / 2
    assert pos(laid_out, "seat.deck")[2] + deck / 2 == pytest.approx(
        cushion_bottom, abs=0.5)


def test_the_back_is_behind_the_seat_and_above_it(spec, laid_out):
    assert pos(laid_out, "back.cushion")[1] > 0
    assert pos(laid_out, "back.cushion")[2] > 410.0


def test_the_back_support_is_behind_the_back_cushion(laid_out):
    """The padded face goes forward; its frame stacks behind it."""
    assert pos(laid_out, "back.support")[1] > pos(laid_out, "back.cushion")[1]


def test_the_arms_are_outside_the_seat_and_above_it(spec, laid_out):
    seat_w, _, _ = extent_of(spec, "seat.cushion")
    left, right = pos(laid_out, "arm.pad", 0), pos(laid_out, "arm.pad", 1)
    assert left[0] < 0 < right[0]
    assert abs(left[0]) >= seat_w / 2
    assert left[2] == pytest.approx(410.0 + 190.0 - 25.0 / 2, abs=1.0)


def test_an_arm_support_does_not_come_out_through_its_pad(spec, laid_out):
    """It hangs from the pad downwards.

    Centred on the armrest height instead, half of a 320mm loop arm stood up
    above the pad it was supposed to be holding - clearly visible, and exactly
    the kind of thing that looks like the whole model is wrong.
    """
    pad_z = pos(laid_out, "arm.pad")[2]
    _, _, pad_t = extent_of(spec, "arm.pad")
    support_z = pos(laid_out, "arm.support")[2]
    span = max(extent_of(spec, "arm.support"))
    assert support_z + span / 2 <= pad_z - pad_t / 2 + 0.5


# --------------------------------------------------------------------------- #
# The base
# --------------------------------------------------------------------------- #

def test_every_castor_sits_on_the_floor(spec, laid_out):
    wheel = max(extent_of(spec, "caster.wheel"))
    for i in range(5):
        assert pos(laid_out, "caster.wheel", i)[2] == pytest.approx(
            wheel / 2, abs=0.5)


def test_the_castors_are_on_the_stated_base_circle(laid_out):
    for i in range(5):
        x, y, _ = pos(laid_out, "caster.wheel", i)
        assert math.hypot(x, y) == pytest.approx(310.0, abs=1.0)


def test_the_castors_are_evenly_spaced(laid_out):
    angles = sorted(
        math.degrees(math.atan2(*reversed(pos(laid_out, "caster.wheel", i)[:2])))
        % 360 for i in range(5))
    gaps = [(b - a) for a, b in zip(angles, angles[1:])]
    for gap in gaps:
        assert gap == pytest.approx(72.0, abs=1.0)


def test_a_spoke_reaches_from_the_hub_to_its_castor(spec, laid_out):
    """Measured so the outer end lands under the wheel.

    Placed at half its own length from the centre instead, a 260mm spoke on a
    310mm base stopped 50mm short and the castors floated past the end of it.
    """
    length = max(extent_of(spec, "base.spoke"))
    x, y, _ = pos(laid_out, "base.spoke")
    assert math.hypot(x, y) + length / 2 == pytest.approx(310.0, abs=1.0)


def test_the_hub_is_at_the_centre_on_the_floor(spec, laid_out):
    _, _, height = extent_of(spec, "base.hub")
    x, y, z = pos(laid_out, "base.hub")
    assert (x, y) == (0.0, 0.0)
    assert z == pytest.approx(height / 2)


def test_the_gas_lift_stands_upright_under_the_seat(laid_out):
    x, y, _ = pos(laid_out, "lift.cylinder")
    assert (x, y) == (0.0, 0.0)
    assert laid_out[("lift.cylinder", 0)][1] != (0.0, 0.0, 0.0)


# --------------------------------------------------------------------------- #
# Nothing left scattered
# --------------------------------------------------------------------------- #

def test_every_instance_is_placed(spec, laid_out):
    assert len(laid_out) == sum(p.get("count", 1) for p in spec["parts"])


def test_nothing_is_left_lying_beside_the_chair(laid_out):
    """The symptom the whole module exists to remove.

    Unrecognised parts fall through to a fallback that lines them up in a row
    beside the object. That is the right behaviour for a part nobody has a rule
    for, and it is what produced a chair as a diagonal line of 27 pieces. If
    anything here lands outside the chair's own envelope, a rule is missing.
    """
    outside = [
        key for key, (position, _) in laid_out.items()
        if math.hypot(position[0], position[1]) > 400
        or not -5 <= position[2] <= 900
    ]
    assert outside == []


def test_nothing_is_underground(laid_out):
    assert all(position[2] >= 0 for position, _ in laid_out.values())
