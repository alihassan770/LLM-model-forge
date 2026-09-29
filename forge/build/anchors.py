"""Turn a part's stated placement into coordinates.

The specification says where a part goes in terms anybody reading a photograph
can answer: near the left edge, on the floor, running front-to-back, one at
each corner. This module is the arithmetic that turns those answers into
millimetres and degrees, against the object's measured bounding box.

Why the split. A vision model asked for "x = -608mm, y = -391mm" is doing
arithmetic it has no way to check, against an origin it cannot see, and it gets
it wrong in ways that look plausible. Asked "which corner, and is it on the
floor?" it is reading the picture, which is the one thing it is reliably good
at. The exactness comes from here: the fractions are multiplied by dimensions
that were measured, so a leg at `anchor_x: 0` lands on the real edge of the
real top, not near it.

The frame, fixed and shared with layout.py:

    X   left to right, origin at the centre of the object
    Y   front to back, origin at the centre, +Y towards the back
    Z   up from the floor, origin ON the floor

Standard library only: layout.py imports this, and layout.py is imported by
code running inside Blender's own Python.
"""

from __future__ import annotations

import math

# Every primitive is built axis-aligned from `box_extent`, which hands back
# (x, y, z) in the part's own frame. So a part already has an orientation
# before it is placed, and `run_axis` is a request to turn it - not to describe
# it from scratch.
AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


def _built_axis(extent: tuple[float, float, float]) -> str:
    """Which way the part's longest side points as it comes out of the builder."""
    x, y, z = extent
    if z >= x and z >= y:
        return "z"
    return "x" if x >= y else "y"


def _is_disc(extent: tuple[float, float, float]) -> bool:
    """Two sides the same and a third clearly smaller - a wheel or a pad.

    The margins are wide because these come from measurements a person wrote
    down: a "round" part is rarely stated as exactly equal on two axes, and a
    flat one only has to be flat enough that its thin side is unmistakable.
    """
    sides = sorted(extent)
    if sides[0] <= 0:
        return False
    return sides[2] <= sides[1] * 1.1 and sides[1] >= sides[0] * 2.0


def _quarter_turn_table() -> dict[tuple[int, int, int], tuple[float, float, float]]:
    """A rotation for every way a box's three axes can be permuted.

    Derived by search rather than written out, because hand-deriving eulers is
    how sign errors get in, and there are only sixty-four combinations of
    quarter turns to try. The first one found for each permutation is kept, so
    the simplest rotation wins.

    Signs are ignored throughout: a box, a cylinder and a sphere are symmetric
    about their centre, so +90 and -90 occupy exactly the same space. Taper and
    a swept profile are the only things that can tell the difference, and being
    end-for-end is a far smaller error than lying on the wrong axis.
    """
    table: dict[tuple[int, int, int], tuple[float, float, float]] = {}
    angles = (0.0, 90.0, 180.0, 270.0)
    for rx in angles:
        for ry in angles:
            for rz in angles:
                x, y, z = (math.radians(rx), math.radians(ry), math.radians(rz))
                cx, sx = math.cos(x), math.sin(x)
                cy, sy = math.cos(y), math.sin(y)
                cz, sz = math.cos(z), math.sin(z)
                matrix = (
                    (cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx),
                    (sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx),
                    (-sy,     cy * sx,                cy * cx),
                )
                # Where each LOCAL axis ends up, as a world axis index.
                mapping = tuple(
                    max(range(3), key=lambda r: abs(matrix[r][c]))
                    for c in range(3)
                )
                if len(set(mapping)) == 3:
                    table.setdefault(mapping, (rx, ry, rz))
    return table


_PERMUTATIONS = _quarter_turn_table()


def orient(extent: tuple[float, float, float], run_axis: str,
           flat_axis: str = "",
           ) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Turn a built part so it sits the way the specification describes it.

    `run_axis` points the part's longest side. `flat_axis` points its
    thinnest - which matters for every panel, and which `run_axis` alone
    cannot express.

    Knowing where the long side goes leaves the other two sides interchangeable,
    and the difference between them is the whole shape of a flat part. A phone's
    front glass is 163 x 77.6 x 0.85: told only that the 163 stands up, the
    remaining quarter turn was chosen arbitrarily and the panel came out 0.85mm
    wide and 77.6mm deep - a phone standing on edge, with its screen facing
    sideways. The audit measured it as 1916% too deep.

    Returns the rotation in degrees and the space the part occupies in world
    axes afterwards, which is what alignment measures against: a 1200mm rail
    turned across the object is 1200mm deep, not 1200mm wide.
    """
    if run_axis not in AXIS_INDEX and flat_axis not in AXIS_INDEX:
        return (0.0, 0.0, 0.0), extent

    longest = max(range(3), key=lambda i: extent[i])
    thinnest = min(range(3), key=lambda i: extent[i])
    if longest == thinnest:                    # a cube: nothing to orient
        return (0.0, 0.0, 0.0), extent

    # On a disc, `run_axis` can only mean the axle.
    #
    # A wheel, a seat pad, a lamp base and a pivot boss are all round and flat:
    # two of their sides are the same diameter, so "which way does the longest
    # side point" has no answer and naming one of those axes says nothing. The
    # single axis that IS distinguishable is the one through the middle, and
    # that is invariably what the axis was meant to name.
    #
    # Read as a long axis instead, a 678mm bicycle wheel told `run_axis: y`
    # was laid flat on the ground like a dinner plate. Read as the axle it
    # stands up, which is the only way a wheel is ever fitted.
    if flat_axis not in AXIS_INDEX and run_axis in AXIS_INDEX and _is_disc(extent):
        flat_axis, run_axis = run_axis, ""

    target: dict[int, int] = {}
    if run_axis in AXIS_INDEX:
        target[longest] = AXIS_INDEX[run_axis]
    if (flat_axis in AXIS_INDEX and thinnest != longest
            and AXIS_INDEX[flat_axis] not in target.values()):
        target[thinnest] = AXIS_INDEX[flat_axis]

    free_local = [i for i in range(3) if i not in target]
    free_world = [i for i in range(3) if i not in target.values()]

    # Anything not named stays exactly where the builder put it, whenever that
    # slot is still free. Pairing the leftovers in order instead would turn a
    # simple swap into a three-way cycle - the same final shape by a needlessly
    # compound rotation, which is harder to reason about in the file.
    #
    # It is tempting to do better than "leave it alone": to assume an object is
    # wider than it is deep, which is true of most things that stand on a
    # floor, and settle the remaining turn that way. That was tried and
    # withdrawn. It fixed a phone's chassis rail and laid every cabinet side
    # panel flat across its carcass, because a side panel is deliberately thin
    # side-to-side and the rule read its 414mm depth as a width.
    #
    # A guess applied where the specification said nothing is precisely the
    # confidently wrong geometry this module exists to avoid. When no alignment
    # names the axis, the part is left as it was built.
    for local in list(free_local):
        if local in free_world:
            target[local] = local
            free_local.remove(local)
            free_world.remove(local)
    for local, world in zip(free_local, free_world):
        target[local] = world

    mapping = tuple(target[i] for i in range(3))
    rotation = _PERMUTATIONS.get(mapping, (0.0, 0.0, 0.0))

    world = [0.0, 0.0, 0.0]
    for local, axis in enumerate(mapping):
        world[axis] = extent[local]
    return rotation, (world[0], world[1], world[2])


def flat_axis(placement: dict) -> str:
    """Which way a panel's thin direction points, read from its alignment.

    A part flush against something is flush across its thickness - that is what
    being flush means. So `align_y: "min"` on a phone's front glass says the
    glass's thin axis runs front-to-back, which is exactly the fact `run_axis`
    cannot carry and which decides whether a panel is a screen or a fin.

    Nothing new is asked of the model for this: the alignment was already being
    given, and already had to be right for the part to sit in the correct
    place. Only one axis may claim it - two flush faces describe a part wedged
    into a corner, which says nothing about which way it is thin.

    Only X and Y are read. `align_z` almost always means something else - a
    leg's foot resting on the floor, a top whose upper surface is the object's
    height, a backrest whose top edge lines up. Those are statements about
    where a part sits vertically, not about which way it is thin, and reading
    a chair's `align_z: "max"` as a thin direction laid its 430 x 60 x 250
    backrest down flat.

    And only for a part that stands UP. A flush face is genuinely ambiguous
    evidence: on a phone's front glass `align_y: "min"` does mean the glass is
    thin front-to-back, and on a shelf the identical words mean its front edge
    is level with the carcass - a statement about where it sits, again.

    What separates them is which way the part runs. A part whose long axis is
    vertical is an upright panel, and the question "which of its other two
    sides is the thickness" is real. A part lying horizontally is already thin
    in Z, because that is how a board is built, and there is nothing to decide.
    Consulting the hint anyway stood a display's 864 x 321 x 25 shelf on edge,
    321mm tall and 160mm through the floor.
    """
    if placement.get("run_axis") != "z":
        return ""
    claimed = [axis for axis in ("x", "y")
               if placement.get(f"align_{axis}") in ("min", "max")]
    return claimed[0] if len(claimed) == 1 else ""


_OPPOSITE = {"min": "max", "max": "min", "center": "center"}


def mirrored_align(how: str) -> str:
    """The alignment a copy needs once it has been reflected.

    Reflecting only the anchor is not a reflection. A leg whose *outer* face
    sits on the left edge is `align_x: "min"`, which pushes it inwards by half
    its section; the same rule applied to its mirror image on the right pushes
    it outwards instead, so one leg is inset and the other overhangs. On a
    1500mm top with a 70mm leg that is 70mm of asymmetry - small enough to look
    like a modelling choice and wrong enough to be useless.

    Reflecting the alignment with the anchor makes the pair symmetrical by
    construction, which is the entire reason patterns exist here.
    """
    return _OPPOSITE.get(how, "center")


def _align(anchor_mm: float, size: float, how: str) -> float:
    """Move from the face the anchor names to the centre the builder needs.

    Blender places an object by its centre. The specification talks about the
    face that touches something - a leg's foot on the floor, a top's upper
    surface at the object's height - because that is what a person can see and
    what actually has to be right. Half a thickness of error here is the
    difference between a shelf resting on its supports and sunk into them.
    """
    if how == "min":
        return anchor_mm + size / 2.0
    if how == "max":
        return anchor_mm - size / 2.0
    return anchor_mm


def _copy(fx: float, fy: float, fz: float, spin: float = 0.0,
          flip_x: bool = False, flip_y: bool = False,
          tilt_x: float = 0.0, tilt_y: float = 0.0) -> dict:
    """One instance: where it goes, how far it is turned, and which way it faces.

    `spin` turns the copy about Z; `tilt_x` and `tilt_y` turn it about the other
    two, which is what a ring standing in a vertical plane needs - the spokes of
    a wheel are each rotated about the axle, not about the vertical.
    """
    return {"fx": fx, "fy": fy, "fz": fz, "spin": spin,
            "flip_x": flip_x, "flip_y": flip_y,
            "tilt_x": tilt_x, "tilt_y": tilt_y}


def _instances(placement: dict, count: int, box: dict,
               world: tuple[float, float, float]) -> list[dict]:
    """One entry per copy of the part.

    The pattern is expanded here rather than asked for copy by copy. Four legs
    described as "one corner, mirrored" are symmetrical because the arithmetic
    makes them so; four legs described as four anchors are symmetrical only if
    the model kept four numbers consistent, and a single digit out of place is
    a leg that visibly does not line up.
    """
    fx = placement["anchor_x"]
    fy = placement["anchor_y"]
    fz = placement["anchor_z"]
    pattern = placement["pattern"]

    if pattern == "mirror_x":
        layout = [_copy(fx, fy, fz),
                  _copy(1.0 - fx, fy, fz, flip_x=True)]
    elif pattern == "mirror_y":
        layout = [_copy(fx, fy, fz),
                  _copy(fx, 1.0 - fy, fz, flip_y=True)]
    elif pattern == "mirror_xy":
        # Ordered so a pair drawn from four copies is a pair across the front,
        # not a diagonal. A chair with two front legs and two back legs of
        # different lengths relies on this.
        layout = [
            _copy(fx, fy, fz),
            _copy(1.0 - fx, fy, fz, flip_x=True),
            _copy(fx, 1.0 - fy, fz, flip_y=True),
            _copy(1.0 - fx, 1.0 - fy, fz, flip_x=True, flip_y=True),
        ]
    elif pattern.startswith("radial"):
        return _radial(placement, count, box, world)
    elif pattern in ("row_x", "row_y", "stack_z"):
        return _row(placement, count, box, pattern)
    else:
        layout = [_copy(fx, fy, fz)]

    return [layout[i % len(layout)] for i in range(count)]


def _row(placement: dict, count: int, box: dict, pattern: str) -> list[dict]:
    """Copies evenly spaced along one axis.

    A stated spacing wins, because a maker's "shelves at 320mm centres" is a
    real measurement. Without one the copies are spread across the object,
    which is right for spindles and slats and roughly right for shelves.
    """
    axis = {"row_x": "x", "row_y": "y", "stack_z": "z"}[pattern]
    span = {"x": box["x_span"], "y": box["y_span"], "z": box["height"]}[axis]
    centre = {"x": placement["anchor_x"], "y": placement["anchor_y"],
              "z": placement["anchor_z"]}[axis]

    if count <= 1:
        offsets = [0.0]
    elif placement["spacing_mm"] > 0 and span > 0:
        step = placement["spacing_mm"] / span
        offsets = [(i - (count - 1) / 2.0) * step for i in range(count)]
    else:
        # Across the middle 80% of the object. Copies pushed right to the
        # edges read as a mistake even when the spacing between them is even.
        offsets = [((i / (count - 1)) - 0.5) * 0.8 for i in range(count)]

    # Keep the row inside the object it belongs to.
    #
    # The anchor is read as the middle of the row, so an anchor naming the
    # FIRST copy throws the rest off the object: a sideboard's three drawer
    # sides were anchored at 0.03 - the left edge, where the first one is -
    # and the row spread outwards from there to -0.37, which is 1.5 metres
    # beyond the end of a 1.8 metre cabinet.
    #
    # An anchor that does not work as the middle is read as the first copy.
    #
    # Both are natural things to mean and the model does not say which. Taken
    # as the middle, a display's four shelves anchored at 0.145 with a 254mm
    # pitch run from -0.07 to 0.36 - the bottom shelf 126mm underground. Taken
    # as the first, they run 254, 508, 762, 1016mm up the carcass, which is
    # exactly where the drawing puts them.
    #
    # So the middle is tried first, because it is the commoner meaning and it
    # is what `stack_z` implies; only when that puts the row outside the object
    # is it re-read as the first copy. Sliding the row to fit instead - the
    # earlier fix - moved these shelves to the floor rather than to the plinth
    # top, because it preserved a centre the model never meant.
    positions = [centre + offset for offset in offsets]
    if min(positions) < 0.0 or max(positions) > 1.0:
        from_first = [centre + (offset - offsets[0]) for offset in offsets]
        if min(from_first) >= 0.0 and max(from_first) <= 1.0:
            positions = from_first

    # Still outside: slide it in by the least that fits, or centre a row too
    # long for the object, because nothing else can be done with it.
    low, high = min(positions), max(positions)
    if high - low > 1.0:
        shift = 0.5 - (low + high) / 2.0
    elif low < 0.0:
        shift = -low
    elif high > 1.0:
        shift = 1.0 - high
    else:
        shift = 0.0

    out = []
    for position in positions:
        fx, fy, fz = (placement["anchor_x"], placement["anchor_y"],
                      placement["anchor_z"])
        if axis == "x":
            fx = position + shift
        elif axis == "y":
            fy = position + shift
        else:
            fz = position + shift
        out.append(_copy(fx, fy, fz))
    return out


# How far out an anchor must sit before its part is treated as a member of the
# outer ring rather than a detail near the hub. A fifth of the radius is a low
# bar on purpose: the arms of a star base are unmistakably outer parts, and a
# boss or a collar around the column sits far inside it.
_RIM_MEMBER_FRACTION = 0.25


def _radial(placement: dict, count: int, box: dict,
            world: tuple[float, float, float]) -> list[dict]:
    """Copies spun about the centre - a five-star chair base, a round pedestal.

    Each copy is also turned to face outwards, which is the whole difference
    between a star base and five identical arms all pointing the same way.
    """
    x_span = box["x_span"] or 1.0
    y_span = box["y_span"] or 1.0

    # The anchor's distance from the centre is the radius the arms sit at. A
    # model that put the anchor at the centre meant "spread them", so a
    # conventional radius is used rather than stacking every copy at the origin.
    dx = (placement["anchor_x"] - 0.5) * x_span
    dy = (placement["anchor_y"] - 0.5) * y_span
    radius = math.hypot(dx, dy)
    if radius < max(x_span, y_span) * 0.05:
        radius = max(x_span, y_span) * 0.35

    # Make the outer ring actually reach the rim.
    #
    # An anchor names where a part *is*, and for an arm the natural reading of
    # that is somewhere around its middle. But a base's diameter is a stated
    # measurement - "62 cm" on the drawing - and the arms are what produce it,
    # so a centre placed by eye leaves the base short by half an arm every
    # time. On the office chair the model anchored the arms sensibly at 0.69,
    # and the 270mm arms still only reached 255mm of the 310mm radius: a base
    # 21% too small, from a specification that had the right number in it.
    #
    # So for a part the anchor already puts out towards the rim, the radius is
    # solved from the reach rather than taken literally: put the tip on the
    # rim and work back. Parts near the hub are left exactly where they were
    # asked to be.
    # Only for an object that is actually round in plan.
    #
    # "The rim" means something on a five-star base or a pedestal, where the
    # footprint is a circle and the outer parts define its diameter. On a
    # bicycle - 440 wide and 1713 long - half the longest side is half the
    # wheelbase, and snapping to it threw 64 spokes out to a 856mm radius,
    # hoops of sticks hanging in the air around the frame.
    object_radius = max(x_span, y_span) / 2.0
    roughly_round = max(x_span, y_span) <= min(x_span, y_span) * 1.4
    if roughly_round and radius > object_radius * _RIM_MEMBER_FRACTION:
        # How far the part sticks out along the radius from its own centre.
        # After `orient` the long side lies on X or Y, so the larger of the two
        # horizontal extents is the one pointing outwards.
        reach = max(world[0], world[1]) / 2.0
        radius = max(0.0, object_radius - reach)

    start = math.atan2(dy, dx) if radius > 0 else 0.0

    # Which axis the copies turn about.
    #
    # A chair base, a pedestal and a round table all spin about Z, and that is
    # the default. A bicycle wheel does not: its spokes radiate about the axle,
    # so they stand in the wheel's own plane. Spun about Z instead, 64 spokes
    # lay flat in a horizontal ring and the bike grew a curtain of vertical
    # sticks between its wheels, 1037mm wide where the drawing says 440.
    spin_axis = placement.get("spin_axis", "z")
    if spin_axis not in AXIS_INDEX:
        spin_axis = "z"

    if spin_axis != "z":
        # The ring turns about its OWN centre, not the object's.
        #
        # Everything above assumes those are the same place, which holds for a
        # star base or a pedestal - the column is at the middle of the object
        # and the arms reach out from it. A wheel is not at the middle of a
        # bicycle: its hub is where the anchor says, and the anchor's distance
        # from the object's centre is half a wheelbase, not a spoke length.
        # Read as a radius it threw the spokes 514mm out in a cone around the
        # whole bike.
        #
        # A spoke reaches from hub to rim, so its own length is the radius -
        # which is a measurement, not an inference.
        radius = max(world) / 2.0

    height = box.get("height") or 1.0
    out = []
    for i in range(count):
        theta = start + (2.0 * math.pi * i / max(1, count))
        cos, sin = math.cos(theta), math.sin(theta)
        degrees = math.degrees(theta)

        if spin_axis == "z":
            # In the plan: copies spread across X and Y, each turned about Z.
            out.append(_copy(0.5 + radius * cos / x_span,
                             0.5 + radius * sin / y_span,
                             placement["anchor_z"], spin=degrees))
        elif spin_axis == "x":
            # In the side elevation: the ring stands up, spread over Y and Z,
            # and every copy keeps the anchor's position across the object -
            # which is what holds a wheel's spokes in one plane.
            out.append(_copy(placement["anchor_x"],
                             placement["anchor_y"] + radius * cos / y_span,
                             placement["anchor_z"] + radius * sin / height,
                             tilt_x=degrees))
        else:
            # In the front elevation: spread over X and Z.
            out.append(_copy(placement["anchor_x"] + radius * cos / x_span,
                             placement["anchor_y"],
                             placement["anchor_z"] + radius * sin / height,
                             tilt_y=degrees))
    return out


def resolve(part: dict, placement: dict, extent: tuple[float, float, float],
            box: dict) -> dict[int, tuple[tuple, tuple]]:
    """Place every copy of one part.

    `extent` is the part's built size from `box_extent`; `box` is the object's
    measured bounding box as `x_span`, `y_span` and `height`, all in
    millimetres. Returns {instance index: ((x, y, z), (rx, ry, rz))} in the
    same units and frame the rest of the builder uses.
    """
    count = max(1, int(part.get("count", 1) or 1))
    base_rotation, world = orient(extent, placement["run_axis"],
                                  flat_axis(placement))

    x_span = box.get("x_span") or 0.0
    y_span = box.get("y_span") or 0.0
    height = box.get("height") or 0.0

    out: dict[int, tuple[tuple, tuple]] = {}
    for index, copy in enumerate(_instances(placement, count, box, world)):
        # A reflected copy reflects its alignment too, or the pair comes out
        # lopsided - see `mirrored_align`.
        align_x = (mirrored_align(placement["align_x"]) if copy["flip_x"]
                   else placement["align_x"])
        align_y = (mirrored_align(placement["align_y"]) if copy["flip_y"]
                   else placement["align_y"])

        # Fractions to millimetres. X and Y are measured from the centre
        # because that is where the object's origin is; Z is measured from the
        # floor, because that is where a floor is.
        x = _align((copy["fx"] - 0.5) * x_span, world[0], align_x)
        y = _align((copy["fy"] - 0.5) * y_span, world[1], align_y)
        z = _align(copy["fz"] * height, world[2], placement["align_z"])

        # A part must not be pushed through the floor by its own alignment.
        # `align_z: center` on something sitting at anchor 0 is a common and
        # harmless slip, and a leg buried half its length underground is a much
        # louder error than one resting on the ground.
        if z - world[2] / 2.0 < -1.0 and placement["anchor_z"] <= 0.02:
            z = world[2] / 2.0

        rotation = (
            base_rotation[0] + placement["tilt_x"] + copy.get("tilt_x", 0.0),
            base_rotation[1] + placement["tilt_y"] + copy.get("tilt_y", 0.0),
            base_rotation[2] + placement["tilt_z"] + copy["spin"],
        )
        out[index] = ((x, y, z), rotation)
    return out
