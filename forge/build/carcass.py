"""Assembling a box: cabinets, sideboards, wardrobes, chests of drawers.

A table is a frame. Four legs at the corners, rails between them, a top laid
over the whole thing, and every part can be placed from the footprint and the
height alone. That is what `layout.py` does, and for a table it is right.

A cabinet is not a frame, it is a **box standing on something**, and almost
nothing about it can be placed from the footprint. Its sides are the full
height of the carcass, its bottom sits at the top of the legs rather than on
the floor, its back closes the rear, and its doors and drawer fronts fill the
opening at the front - an opening whose size depends on the thickness of the
panels around it. Placing those parts against a table's assumptions gives
correctly-sized panels in meaningless positions, which is exactly what the
first sideboard build produced: 63 parts, every dimension right, scattered.

So this module works out the box first - where it starts, how tall it is, how
thick its walls are - and then places each part against that box.

Everything is derived from the specification rather than assumed. A carcass
whose parts do not state a thickness gets a conventional 18mm, and that
assumption is visible here rather than buried in six places.

Imported by `layout.py`, which runs inside Blender, so the standard library is
all that is available.
"""

from __future__ import annotations

from dataclasses import dataclass

# Conventional panel thickness for a cabinet, in millimetres, used only when
# the specification does not say. 18mm is what sheet goods come in.
DEFAULT_WALL = 18.0

# How far a door or drawer front is held back from the carcass front face.
# Zero would leave it exactly flush; a hair of inset stops the two faces
# z-fighting in a render, where identical coplanar surfaces flicker.
FRONT_INSET = 0.5


@dataclass(frozen=True)
class Box:
    """The carcass: an open box standing at `base_z`.

    `x_span` and `y_span` are its outside width and depth, `height` is the
    height of its sides, and `wall` is how thick those sides are. Everything
    else in this module is positioned from these five numbers.
    """

    x_span: float
    y_span: float
    base_z: float      # top of the legs or plinth; where the carcass begins
    height: float      # height of the side panels
    wall: float        # thickness of the side panels
    floor: float       # thickness of the bottom panel

    @property
    def top_z(self) -> float:
        """Underside of whatever surface caps the carcass."""
        return self.base_z + self.height

    @property
    def inner_width(self) -> float:
        """Clear width between the two side panels."""
        return max(1.0, self.x_span - 2 * self.wall)

    @property
    def opening_bottom(self) -> float:
        """Top face of the bottom panel: where the usable opening starts."""
        return self.base_z + self.floor

    @property
    def opening_height(self) -> float:
        return max(1.0, self.top_z - self.opening_bottom)

    @property
    def front_y(self) -> float:
        """Y of the carcass front face. Negative: the front faces -Y."""
        return -self.y_span / 2

    @property
    def back_y(self) -> float:
        return self.y_span / 2


def looks_like_a_carcass(parts: list[dict]) -> bool:
    """Does this object have a box in it?

    A carcass announces itself with panels that only a box has: a side, a
    back, a bottom. One of those could be a coincidence - a desk has a modesty
    panel - so two are required, and a door or a drawer front on its own is
    not enough because a table can have a drawer in its apron.
    """
    ids = " ".join(p.get("id", "").lower() for p in parts)
    signals = sum(bool(s) for s in (
        "carcass" in ids,
        "side.panel" in ids or "side_panel" in ids or "panel.side" in ids,
        "back.panel" in ids or "back_panel" in ids or "carcass.back" in ids,
        "divider" in ids,
        "plinth" in ids,
    ))
    return signals >= 2


def measure(parts: list[dict], frame: dict, axes) -> Box:
    """Work out the box from the parts that make it.

    Read from the specification wherever it says, because a specification that
    states a 645mm side panel and a 150mm leg is telling us exactly where the
    carcass starts and stops. Falling back to proportions of the overall height
    would throw that away and produce a cabinet that is nearly right, which is
    harder to spot than one that is obviously wrong.
    """
    def find(*needles):
        for part in parts:
            pid = part.get("id", "").lower()
            if any(n in pid for n in needles):
                return part
        return None

    side = find("carcass.side", "side.panel", "side_panel", "panel.side")
    bottom = find("carcass.bottom", "bottom.panel", "base.panel", "carcass.base")
    leg = find("leg", "post", "plinth", "foot.")

    wall = (axes(side).get("thickness") if side else None) or DEFAULT_WALL
    floor = (axes(bottom).get("thickness") if bottom else None) or wall

    # The carcass starts at the top of whatever holds it up.
    base_z = 0.0
    if leg is not None:
        base_z = axes(leg).get("height") or axes(leg).get("length") or 0.0

    # Its height is the side panel's, which is the one measurement that says
    # so directly.
    height = (axes(side).get("height") if side else 0.0) or 0.0
    if not height:
        # No side panel stated: what is left of the object once the legs and
        # the top are taken off.
        height = max(
            1.0,
            frame["height"] - base_z - frame.get("top_thickness", 25.0),
        )

    return Box(
        x_span=frame["x_span"],
        y_span=frame["y_span"],
        base_z=base_z,
        height=height,
        wall=wall,
        floor=floor,
    )


def _row_positions(count: int, span: float) -> list[float]:
    """Centres for `count` items sharing `span`, evenly, centred on zero.

    Two doors across 1764mm land at -441 and +441, which is where a maker
    would put them: each door owns half the opening and sits in the middle of
    its half.
    """
    if count < 1:
        return []
    width = span / count
    return [-span / 2 + width * (i + 0.5) for i in range(count)]


def _boundaries(count: int, span: float) -> list[float]:
    """The lines *between* `count` bays: where a divider goes.

    Three drawer bays across 1764mm have two dividers, at -294 and +294.
    """
    if count < 2:
        return []
    width = span / count
    return [-span / 2 + width * (i + 1) for i in range(count - 1)]


def _arrangement(count: int, item_width: float, inner: float) -> str:
    """Are these fronts side by side, or stacked one above another?

    Three 570mm drawer fronts in a 1764mm cabinet are a bank across the width.
    Four 764mm drawer fronts in the same cabinet are a chest, stacked. The
    widths say which without anyone having to label it: laid side by side they
    either fit or they do not.
    """
    if count < 2:
        return "across"
    return "across" if item_width * count <= inner * 1.15 else "stacked"


def place(parts: list[dict], box: Box, frame: dict, axes, extent) -> dict:
    """Position every part of the box. Returns {(part_id, index): (pos, rot)}.

    Parts this does not recognise are left out, so the caller can fall back to
    its own rules for them. Legs, glides and anything else below the carcass
    stay with the frame rules, which already put them at the corners.
    """
    out: dict = {}

    def claim(part, index, pos, rot=(0.0, 0.0, 0.0)):
        out[(part["id"], index)] = (pos, rot)

    def find_all(*needles):
        return [p for p in parts
                if any(n in p.get("id", "").lower() for n in needles)]

    def count_of(part):
        return max(1, int(part.get("count", 1)))

    # ---------------------------------------------------------------- shell
    for part in find_all("carcass.side", "side.panel", "side_panel", "panel.side"):
        x, y, z = extent(part)
        # Built lying front-to-back; a Z rotation stands it on edge as a side.
        offset = box.x_span / 2 - box.wall / 2
        for i in range(count_of(part)):
            claim(part, i, (-offset if i % 2 == 0 else offset,
                            0.0, box.base_z + box.height / 2), (0.0, 0.0, 90.0))

    for part in find_all("carcass.bottom", "bottom.panel", "carcass.base", "base.panel"):
        _, _, t = extent(part)
        for i in range(count_of(part)):
            claim(part, i, (0.0, 0.0, box.base_z + t / 2))

    for part in find_all("carcass.top", "top.panel"):
        _, _, t = extent(part)
        for i in range(count_of(part)):
            claim(part, i, (0.0, 0.0, box.top_z - t / 2))

    for part in find_all("carcass.back", "back.panel", "back_panel", "panel.back"):
        _, t, _ = extent(part)
        for i in range(count_of(part)):
            claim(part, i, (0.0, box.back_y - t / 2,
                            box.base_z + box.height / 2))

    # ------------------------------------------------------------ the front
    #
    # The opening is divided into rows. Doors take the bottom and drawers sit
    # above them, which is how a sideboard is built and the only order the
    # specification's own "fixed shelf under drawer bank" is consistent with.
    doors = find_all("door")
    doors = [p for p in doors if not any(
        w in p["id"].lower() for w in ("handle", "hinge", "knob", "pull"))]
    fronts = [p for p in find_all("drawer.front", "front.drawer")]

    cursor = box.opening_bottom
    rows: dict[str, tuple[float, float]] = {}   # name -> (z_centre, height)
    # Where each front ended up: (x, z, width, height). A drawer box, a runner,
    # a handle and a hinge are all positioned relative to the front they serve,
    # so guessing their place separately would be inventing a second answer to
    # a question already settled.
    door_slots: list[tuple[float, float, float, float]] = []
    drawer_slots: list[tuple[float, float, float, float]] = []

    for part in doors:
        w, d, h = extent(part)
        n = count_of(part)
        how = _arrangement(n, w, box.inner_width)
        if how == "across":
            z = cursor + h / 2
            for i, x in enumerate(_row_positions(n, box.inner_width)):
                claim(part, i, (x, box.front_y + d / 2 + FRONT_INSET, z))
                door_slots.append((x, z, w, h))
            rows["door"] = (z, h)
            cursor += h
        else:
            for i in range(n):
                z = cursor + h / 2 + i * h
                claim(part, i, (0.0, box.front_y + d / 2 + FRONT_INSET, z))
                door_slots.append((0.0, z, w, h))
            rows["door"] = (cursor + (h * n) / 2, h * n)
            cursor += h * n

    # A fixed shelf between the rows takes up room. Placing it at the boundary
    # without advancing past it left it overlapping the row above by its own
    # thickness: physically two objects in one place.
    divider_shelf = next(
        (x for x in find_all("shelf")
         if "fix" in x["id"].lower() or "drawer" in x["id"].lower()), None)
    if divider_shelf is not None and doors and fronts:
        _, _, shelf_t = extent(divider_shelf)
        for i in range(count_of(divider_shelf)):
            claim(divider_shelf, i, (0.0, 0.0, cursor + shelf_t / 2))
        rows["shelf"] = (cursor + shelf_t / 2, shelf_t)
        cursor += shelf_t

    for part in fronts:
        w, d, h = extent(part)
        n = count_of(part)
        how = _arrangement(n, w, box.inner_width)
        if how == "across":
            z = cursor + h / 2
            for i, x in enumerate(_row_positions(n, box.inner_width)):
                claim(part, i, (x, box.front_y + d / 2 + FRONT_INSET, z))
                drawer_slots.append((x, z, w, h))
            rows["drawer"] = (z, h)
            rows["drawer_bays"] = (float(n), z)
            cursor += h
        else:
            for i in range(n):
                z = cursor + h / 2 + i * h
                claim(part, i, (0.0, box.front_y + d / 2 + FRONT_INSET, z))
                drawer_slots.append((0.0, z, w, h))
            rows["drawer"] = (cursor + (h * n) / 2, h * n)
            cursor += h * n

    # ------------------------------------------------------- shelves inside
    #
    # A fixed shelf separates two rows, so it belongs at the boundary the
    # fronts have just established rather than at a fraction of the height.
    # Matching is on substrings, so "shelf" also catches "pin.shelf" and
    # "bracket.shelf" - the hardware that *holds* a shelf, not a shelf. The
    # pin loop below overwrites these placements anyway, so this guard is not
    # what keeps the pins right; it is here so the intermediate state is not a
    # lie. Reading `out` back for "everything shelf-shaped" is how the pin loop
    # came to treat the pins' own placeholder positions as shelf heights, and
    # leaving that bait lying around invites the same mistake again. Same guard
    # the door loop already uses, for the same reason.
    shelf_levels: list[tuple[float, float]] = []   # (z centre, thickness)

    for part in find_all("shelf"):
        if any(w in part["id"].lower()
               for w in ("pin", "stud", "bracket", "support", "peg")):
            continue
        if (part["id"], 0) in out:      # the row divider, already placed
            continue
        _, _, t = extent(part)
        n = count_of(part)
        pid = part["id"].lower()
        fixed = "fix" in pid or "drawer" in pid
        if fixed and "door" in rows:
            z = rows["door"][0] + rows["door"][1] / 2 + t / 2
            for i in range(n):
                claim(part, i, (0.0, 0.0, z))
        else:
            # Adjustable shelves divide whatever space is left below the
            # topmost row, evenly.
            low = box.opening_bottom
            high = rows.get("door", (box.top_z, 0.0))[0] + rows.get("door", (0, 0))[1] / 2
            span = max(1.0, high - low)
            for i in range(n):
                z = low + span * (i + 1) / (n + 1)
                claim(part, i, (0.0, 0.0, z))
                shelf_levels.append((z, t))

    # ------------------------------------------------------------ dividers
    #
    # A divider stands between bays, and its height says which row it is in:
    # a 186mm divider belongs to the drawer bank, a 423mm one to the cupboard.
    for part in find_all("divider", "muntin", "mullion"):
        _, _, h = extent(part)
        n = count_of(part)
        nearest = min(
            (r for r in ("door", "drawer") if r in rows),
            key=lambda r: abs(rows[r][1] - h),
            default=None,
        )
        z = rows[nearest][0] if nearest else box.base_z + box.height / 2
        bays = int(rows["drawer_bays"][0]) if (nearest == "drawer"
                                              and "drawer_bays" in rows) else n + 1
        lines = _boundaries(bays, box.inner_width) or _boundaries(n + 1, box.inner_width)
        for i in range(n):
            x = lines[i % len(lines)] if lines else 0.0
            claim(part, i, (x, 0.0, z), (0.0, 0.0, 90.0))

    # ------------------------------------------- what hangs off each front
    #
    # A drawer box, its runners, a handle and a hinge all belong to a front
    # that has already been placed. Positioning them from the carcass again
    # would be answering a settled question a second time, and the two answers
    # would drift apart the moment either rule changed.

    def spread(slots, part, index, dy=0.0, dz=0.0, per_slot=1, rot=None):
        """Place `part` once per slot, in slot order, offset from the front."""
        if not slots:
            return False
        slot = slots[(index // per_slot) % len(slots)]
        x, z, w, h = slot
        claim(part, index, (x, box.front_y + dy, z + dz), rot or (0.0, 0.0, 0.0))
        return True

    for part in find_all("handle", "knob", "pull"):
        pw, pd, ph = extent(part)
        pid = part["id"].lower()
        slots = drawer_slots if "drawer" in pid else door_slots
        if not slots:
            continue
        for i in range(count_of(part)):
            x, z, w, h = slots[i % len(slots)]
            # A door handle goes near the opening edge, a drawer handle in the
            # middle of its front.
            # A bar handle runs across a drawer and up a door: that is simply
            # how they are fitted, and it is also the only way round that fits
            # - a 160mm bar lying flat on a 417mm door would read as a drawer.
            rot = (0.0, 0.0, 0.0)
            if "door" in pid:
                rot = (0.0, 90.0, 0.0)
                if len(slots) > 1:
                    # On the opening edge, which on a pair of doors is the one
                    # facing the centre. The hinges take the other edge, and
                    # putting both on the same side is how you get a door that
                    # cannot open.
                    opening_edge = x - w / 2 if x > 0 else x + w / 2
                    x = opening_edge + (w * 0.12 if x > 0 else -w * 0.12)
            claim(part, i, (x, box.front_y - pd / 2 - 4.0, z), rot)

    for part in find_all("hinge"):
        hw, hd, hh = extent(part)
        if not door_slots:
            continue
        per_door = max(1, count_of(part) // max(1, len(door_slots)))
        for i in range(count_of(part)):
            x, z, w, h = door_slots[(i // per_door) % len(door_slots)]
            # On the outer stile of its door, set in from top and bottom.
            edge = x + (w / 2 if x > 0 else -w / 2)
            which = i % per_door
            offset = h / 2 - 70.0
            dz = -offset if which == 0 else offset
            claim(part, i, (edge, box.front_y + box.wall, z + dz))

    side_part = next((x for x in find_all("drawer.side") ), None)
    box_depth = extent(side_part)[0] if side_part is not None else 0.0
    front_depth = extent(fronts[0])[1] if fronts else DEFAULT_WALL

    for part in find_all("drawer.side", "drawer.back", "drawer.base",
                         "drawer.bottom", "runner"):
        pw, pd, ph = extent(part)
        pid = part["id"].lower()
        if not drawer_slots:
            continue
        per_drawer = max(1, count_of(part) // max(1, len(drawer_slots)))
        # How deep the box runs, from the sides that form it. Using the back
        # panel's own width here put the back 246mm from centre in a cabinet
        # only 225mm deep - the width of a part is not the depth of the box it
        # sits in.
        depth = box_depth or (box.y_span - front_depth - 20.0)
        near = box.front_y + front_depth          # just behind the front
        centre_y = near + depth / 2
        for i in range(count_of(part)):
            x, z, w, h = drawer_slots[(i // per_drawer) % len(drawer_slots)]
            if "side" in pid or "runner" in pid:
                # Two per drawer, one down each side of the box.
                side = -1 if (i % 2 == 0) else 1
                offset = w / 2 - pd / 2 if "side" in pid else w / 2 + pd
                claim(part, i, (x + side * offset, centre_y, z),
                      (0.0, 0.0, 90.0))
            elif "back" in pid:
                claim(part, i, (x, near + depth - pd / 2, z))
            else:  # base
                claim(part, i, (x, centre_y, z - h / 2 + ph / 2))

    # Four pins carry one shelf: one at each corner, sunk into the side panel
    # and reaching under the shelf it holds. Both of those have to be true or
    # the pin is a loose rod floating in the cabinet.
    for part in find_all("pin.shelf", "shelf.pin", "pin"):
        pw, pd, ph = extent(part)
        n = count_of(part)
        levels = shelf_levels or [(box.base_z + box.height / 2, 18.0)]
        for i in range(n):
            z_shelf, thickness = levels[(i // 4) % len(levels)]
            side = -1 if (i % 4) in (0, 3) else 1
            front = -1 if (i % 4) in (0, 1) else 1
            claim(part, i, (side * (box.inner_width / 2 - pw / 2),
                            front * (box.y_span / 2 - 60.0),
                            z_shelf - thickness / 2 - ph / 2))

    # -------------------------------------------------- base rails, plinths
    #
    # These sit between the legs, at the bottom. The frame rules would put a
    # rail under the *top*, which is where a table's apron goes and nowhere a
    # cabinet has anything.
    for part in find_all("rail", "plinth", "stretcher"):
        w, d, h = extent(part)
        n = count_of(part)
        along_x = w >= box.x_span * 0.5
        # Built lying flat: stand it on edge so it reads as a rail.
        rot = (90.0, 0.0, 0.0) if h < d else (0.0, 0.0, 0.0)
        stand = max(h, d) if h < d else h
        for i in range(n):
            if along_x:
                offset = box.y_span / 2 - max(d, h) / 2
                claim(part, i, (0.0, -offset if i % 2 == 0 else offset,
                                box.base_z - stand / 2), rot)
            else:
                offset = box.x_span / 2 - max(d, h) / 2
                claim(part, i, (-offset if i % 2 == 0 else offset, 0.0,
                                box.base_z - stand / 2),
                      (rot[0], rot[1], 90.0))

    return out
