"""Assembling a chair: seat, back, arms, and whatever holds it off the floor.

A chair is the third shape this app has to know about, and it is not a frame
and not a box.

`layout.py` builds a *frame* - legs at the corners of a footprint, rails
between them, a top over the lot. `carcass.py` builds a *box* - panels around
an opening, fronts filling it. A chair is neither. It is a seat at a stated
height with things attached to it: a back rising from its rear edge, arms
standing either side, and underneath either four legs or a single column on a
star base. Almost nothing about it can be derived from a footprint, because its
footprint is the base, its seat is smaller than that, and its back is not on
the floor at all.

Run through the frame rules a chair produced exactly what the sideboard did
before `carcass.py`: 27 correctly-sized parts in a diagonal line. Nothing was
the wrong size. The extraction had read every label off the drawing correctly.
The assembly simply had no idea what a chair was.

So this module finds the seat first, and places everything else relative to it.

Two sub-types share all of that and differ only below the seat:

* a **pedestal** chair - one column on a hub with radiating spokes and a castor
  at the end of each, which is every office chair;
* a **legged** chair - four legs at the corners of the seat, which is every
  dining chair.

Which one is decided by the parts present, not by a label, because a spec does
not reliably carry one.

Imported by `layout.py`, which runs inside Blender, so the standard library is
all that is available.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Where the seat sits when nothing says. 450mm is the height of essentially
# every chair made for a table, and being explicit here beats a magic number
# appearing in six placements.
DEFAULT_SEAT_HEIGHT = 450.0

# How far the back leans off vertical, in degrees. A back built dead upright
# reads as a box on a stool; every real chair has some rake.
BACK_RAKE = 7.0

# A gap left between the seat surface and the bottom of the back, so the two
# do not intersect when the rake tips the back forward at its base.
BACK_CLEARANCE = 8.0


@dataclass(frozen=True)
class Chair:
    """The seat, and everything measured from it.

    `seat_top` is the height of the sitting surface - the one dimension a spec
    for a chair almost always states, and the one everything else hangs off.
    """

    seat_width: float       # side to side
    seat_depth: float       # front to back
    seat_top: float         # floor to the top of the seat
    seat_thickness: float
    back_height: float
    arm_height: float       # above the seat surface
    base_radius: float      # castor centre to chair centre
    pedestal: bool          # column on a star base, or legs

    @property
    def seat_bottom(self) -> float:
        return max(1.0, self.seat_top - self.seat_thickness)

    @property
    def seat_front(self) -> float:
        """Y of the front edge. Negative: the front faces -Y, as elsewhere."""
        return -self.seat_depth / 2

    @property
    def seat_back(self) -> float:
        return self.seat_depth / 2


def _ids(parts: list[dict]) -> str:
    return " ".join(p.get("id", "").lower() for p in parts)


def looks_like_seating(parts: list[dict]) -> bool:
    """Is there a seat, and something that makes it a chair rather than a box?

    A seat on its own could be a bench top or a box lid, so one corroborating
    sign is required: a back, an arm, a castor, a star base or a gas lift.
    Those are things only seating has.
    """
    ids = _ids(parts)
    if "seat" not in ids and "cushion" not in ids:
        return False
    return any(word in ids for word in (
        "back", "arm", "castor", "caster", "spoke", "hub",
        "lift", "gas", "pedestal", "swivel", "splat", "stile",
    ))


def _is_pedestal(parts: list[dict]) -> bool:
    ids = _ids(parts)
    return any(w in ids for w in
               ("spoke", "hub", "castor", "caster", "lift", "gas", "pedestal"))


def measure(parts: list[dict], named: dict[str, float], axes, extent) -> Chair:
    """Work out the chair from the spec, preferring what it states outright.

    `named` is the spec's own overall measurements by name. A chair drawing
    labels the things that matter - seat height, base diameter, armrest height
    above the seat - and using those is the difference between a chair and a
    chair-shaped guess. Falling back to proportions of the total height would
    throw away the most reliable input on the page.
    """
    def stated(*needles, default=0.0):
        for needle in needles:
            if needle in named:
                return named[needle]
        for needle in needles:
            for name, value in named.items():
                if needle in name:
                    return value
        return default

    def find(*needles):
        for part in parts:
            pid = part.get("id", "").lower()
            if any(n in pid for n in needles):
                return part
        return None

    seat = find("seat.cushion", "seat.pad", "seat")
    back = find("back.cushion", "back.rest", "backrest", "back")
    arm = find("arm.pad", "armrest", "arm")

    seat_w = seat_d = seat_t = 0.0
    if seat is not None:
        seat_w, seat_d, seat_t = extent(seat)
    seat_w = seat_w or stated("seat_width", default=460.0)
    seat_d = seat_d or stated("seat_depth", default=430.0)
    seat_t = seat_t or 60.0

    # The lowest stated seat height. A gas lift gives a range; a chair modelled
    # at the bottom of its travel is the one that matches the drawing's
    # elevation, and it is also the only end of the range that is a fact rather
    # than a sum.
    seat_top = stated("seat_height_min", "seat_height", "seat height",
                      default=DEFAULT_SEAT_HEIGHT)

    back_h = extent(back)[2] if back is not None else 0.0
    back_h = back_h or stated("backrest_height", "back_height", default=400.0)

    arm_h = stated("armrest_height_above_seat", "armrest_height", "arm_height",
                   default=0.0)
    if not arm_h and arm is not None:
        arm_h = 190.0

    base_d = stated("base_diameter", "base diameter", default=0.0)
    base_r = (base_d / 2) if base_d else max(seat_w, seat_d) * 0.7

    return Chair(
        seat_width=seat_w,
        seat_depth=seat_d,
        seat_top=seat_top,
        seat_thickness=seat_t,
        back_height=back_h,
        arm_height=arm_h,
        base_radius=base_r,
        pedestal=_is_pedestal(parts),
    )


def _upright(extent_xyz: tuple[float, float, float]) -> tuple:
    """Rotation that stands a part's longest axis vertical.

    A gas lift is stated as "length 240, diameter 50", and box_extent reads
    that as 240 along X - correct for a bar, wrong for a column. The parts that
    need this are the ones a spec describes by length while meaning height.
    """
    x, y, z = extent_xyz
    if z >= x and z >= y:
        return (0.0, 0.0, 0.0)
    if x >= y:
        return (0.0, 90.0, 0.0)
    return (90.0, 0.0, 0.0)


def place(parts: list[dict], chair: Chair, extent) -> dict:
    """Position every part of the chair. Returns {(part_id, index): (pos, rot)}.

    Anything not recognised is left unclaimed, so `layout.py` can fall back to
    its own rules for it.
    """
    out: dict = {}

    def claim(part, index, pos, rot=(0.0, 0.0, 0.0)):
        out[(part["id"], index)] = (pos, rot)

    def find_all(*needles):
        return [p for p in parts
                if any(n in p.get("id", "").lower() for n in needles)]

    def count_of(part):
        return max(1, int(part.get("count", 1)))

    claimed_ids: set[str] = set()

    def take(*needles, exclude=()):
        """Parts matching these names that nothing else has claimed yet.

        Order matters here in a way it does not in `carcass.py`: "arm" matches
        "arm.pad" and "arm.support", and "back" matches "back.cushion" and
        "back.support", so each loop has to remove what it used or the next one
        places the same part again somewhere else.
        """
        found = []
        for part in find_all(*needles):
            pid = part["id"].lower()
            if part["id"] in claimed_ids:
                continue
            if any(w in pid for w in exclude):
                continue
            found.append(part)
            claimed_ids.add(part["id"])
        return found

    # ------------------------------------------------------------- the seat
    #
    # Everything else is measured from this, so it goes down first.
    seat_parts = take("seat", "squab", exclude=("mechanism", "lift", "runner"))
    # Cushion on top, deck under it, any frame under that: a seat is a stack,
    # and the softest thing is the part you sit on.
    seat_parts.sort(key=lambda p: (0 if "cushion" in p["id"].lower()
                                   else 1 if "pad" in p["id"].lower()
                                   else 2))
    cursor = chair.seat_top
    for part in seat_parts:
        _, _, thickness = extent(part)
        for i in range(count_of(part)):
            claim(part, i, (0.0, 0.0, cursor - thickness / 2))
        cursor -= thickness
    seat_underside = cursor

    # ------------------------------------------------------------- the back
    #
    # Rising from the rear edge of the seat, raked back a few degrees, with the
    # padded face forward and its supports stacked behind it.
    back_parts = take("back", "splat", "lumbar",
                      exclude=("bracket", "plate", "base"))
    back_parts.sort(key=lambda p: (0 if "cushion" in p["id"].lower()
                                   else 1 if "pad" in p["id"].lower()
                                   else 2))
    depth_cursor = chair.seat_back
    for part in back_parts:
        _, thickness, height = extent(part)
        # Raking the back tips its base forward, so it is lifted by the amount
        # the rotation swings it down. Without this the bottom corner of the
        # back cuts through the seat.
        lean = math.sin(math.radians(BACK_RAKE)) * height / 2
        z = chair.seat_top + BACK_CLEARANCE + height / 2 + lean
        for i in range(count_of(part)):
            claim(part, i, (0.0, depth_cursor + thickness / 2, z),
                  (-BACK_RAKE, 0.0, 0.0))
        depth_cursor += thickness

    # ------------------------------------------------------------- the arms
    #
    # One either side, at the stated height above the seat. Pads run front to
    # back, which is the way an arm is used and the opposite of the way a spec
    # states it: "length 260" is read onto X by box_extent, so the pad has to
    # be turned a quarter turn to lie along the chair.
    arm_top = chair.seat_top + (chair.arm_height or 190.0)
    # Read before the loop: a support is placed relative to the underside of
    # the pad, and the pad may come after it in the parts list.
    _pad = next((p for p in parts if "arm" in p["id"].lower()
                 and any(w in p["id"].lower() for w in ("pad", "rest", "top"))),
                None)
    _pad_thickness = extent(_pad)[2] if _pad is not None else 25.0

    for part in take("arm"):
        pid = part["id"].lower()
        length, width, thickness = extent(part)
        n = count_of(part)
        for i in range(n):
            side = -1 if i % 2 == 0 else 1
            if "pad" in pid or "rest" in pid or "top" in pid:
                claim(part, i,
                      (side * (chair.seat_width / 2 + width / 2),
                       0.0, arm_top - thickness / 2),
                      (0.0, 0.0, 90.0))
            else:
                # The support under the pad, stood upright just outside the
                # seat. Hung from the *top* down rather than centred on a
                # guessed height: a loop arm is stated as a single length of
                # tube - 320mm of it here - and centring that on the armrest
                # height sent half of it up through the pad it is supposed to
                # hold. Its far end running down past the seat is right; that
                # is where a loop arm returns to the frame.
                span = max((length, width, thickness))
                pad_underside = arm_top - _pad_thickness
                claim(part, i,
                      (side * (chair.seat_width / 2 + width / 2),
                       0.0, pad_underside - span / 2),
                      _upright((length, width, thickness)))

    # ----------------------------------------------------- under the seat
    #
    # The mechanism a pedestal chair hangs on: a plate bolted under the seat,
    # then the column down to the base.
    for part in take("mechanism", "seat.plate", "tilt"):
        _, _, thickness = extent(part)
        for i in range(count_of(part)):
            claim(part, i, (0.0, 0.0, seat_underside - thickness / 2))
        seat_underside -= thickness

    column_top = seat_underside
    hub_height = 0.0
    hub = next((p for p in parts if "hub" in p["id"].lower()), None)
    if hub is not None:
        hub_height = extent(hub)[2]

    for part in take("lift", "gas", "column", "pedestal", "spindle"):
        pid = part["id"].lower()
        size = extent(part)
        if "lever" in pid or "handle" in pid or "paddle" in pid:
            # The height adjustment lever, out to one side under the seat.
            length, width, thickness = size
            for i in range(count_of(part)):
                claim(part, i,
                      (chair.seat_width / 2 * 0.8, chair.seat_front * 0.4,
                       seat_underside - thickness),
                      (0.0, 0.0, 20.0))
            continue
        # The column itself: from the top of the hub up to the seat.
        length = max(size)
        for i in range(count_of(part)):
            claim(part, i, (0.0, 0.0, max(hub_height, column_top - length / 2)),
                  _upright(size))

    # ------------------------------------------------------------- the base
    if chair.pedestal:
        spokes = take("spoke", "star", "base.arm")
        castors = take("castor", "caster", "wheel", "glide")
        forks = [p for p in castors if "fork" in p["id"].lower()
                 or "stem" in p["id"].lower() or "housing" in p["id"].lower()]
        wheels = [p for p in castors if p not in forks]

        arms = max((count_of(p) for p in spokes), default=0) or \
            max((count_of(p) for p in wheels), default=5)
        angles = [i * 360.0 / arms for i in range(arms)]

        for part in take("hub", "base.centre", "base.center") + \
                ([hub] if hub is not None and hub["id"] not in claimed_ids
                 else []):
            _, _, height = extent(part)
            claimed_ids.add(part["id"])
            for i in range(count_of(part)):
                claim(part, i, (0.0, 0.0, height / 2))

        for part in spokes:
            length, width, height = extent(part)
            # Positioned so the *outer* end reaches the castors, not so the
            # inner end sits at the centre. A 260mm spoke on a base with a
            # 310mm castor radius starts at the rim of the hub and ends under
            # the wheel; measuring from the centre instead left every spoke
            # stopping 50mm short, with the castors floating beyond them.
            offset = max(length / 2, chair.base_radius - length / 2)
            for i in range(count_of(part)):
                angle = angles[i % len(angles)]
                radians = math.radians(angle)
                claim(part, i,
                      (math.cos(radians) * offset,
                       math.sin(radians) * offset,
                       height / 2),
                      (0.0, 0.0, angle))

        for part in wheels:
            diameter = max(extent(part)[0], extent(part)[1], extent(part)[2])
            for i in range(count_of(part)):
                angle = angles[i % len(angles)]
                radians = math.radians(angle)
                claim(part, i,
                      (math.cos(radians) * chair.base_radius,
                       math.sin(radians) * chair.base_radius,
                       diameter / 2),
                      # A wheel turns about a horizontal axis, across the
                      # direction its spoke points.
                      (90.0, 0.0, angle))

        for part in forks:
            _, _, height = extent(part)
            diameter = 50.0
            for i in range(count_of(part)):
                angle = angles[i % len(angles)]
                radians = math.radians(angle)
                claim(part, i,
                      (math.cos(radians) * chair.base_radius,
                       math.sin(radians) * chair.base_radius,
                       diameter + height / 2),
                      (0.0, 0.0, angle))
    else:
        # A legged chair: four legs at the corners of the seat, reaching the
        # floor, with any stretchers between them near the bottom.
        inset = 0.90
        corners = [(-1, -1), (1, -1), (-1, 1), (1, 1)]
        for part in take("leg", "post"):
            width, depth, _ = extent(part)
            for i in range(count_of(part)):
                sx, sy = corners[i % 4]
                claim(part, i,
                      (sx * (chair.seat_width / 2 * inset - width / 2),
                       sy * (chair.seat_depth / 2 * inset - depth / 2),
                       seat_underside / 2))

        for part in take("stretcher", "rail", "apron"):
            width, depth, height = extent(part)
            along_x = width >= depth
            for i in range(count_of(part)):
                side = -1 if i % 2 == 0 else 1
                if along_x:
                    claim(part, i,
                          (0.0, side * chair.seat_depth / 2 * inset,
                           seat_underside - height / 2))
                else:
                    claim(part, i,
                          (side * chair.seat_width / 2 * inset, 0.0,
                           seat_underside - height / 2),
                          (0.0, 0.0, 90.0))

    return out
