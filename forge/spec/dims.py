"""Resolving a part's dimensions - the one place it is done.

Both the geometry builder and the bill of materials need to answer "how big is
this part?", and they **must** answer identically. When they did not, a leg was
built 70 × 70 × 735mm and costed as 800 × 735 × 70mm, putting the estimated
weight at 170kg instead of ~45kg. The model spotted it in its advice; the code
had no way to.

Sharing this module is what makes "the bill cannot disagree with the model" a
structural property rather than a promise.

Imported by `forge/build/build.py`, which runs inside Blender, so it must
depend on nothing but the standard library.
"""

from __future__ import annotations

import math

TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}

# Axis names, longest synonym list first so "cross-section width" matches
# width rather than falling through.
AXIS_SYNONYMS = {
    "length": ("length", "len", "long", "span"),
    "width": ("width", "wide", "breadth"),
    "depth": ("depth", "deep"),
    "height": ("height", "tall", "high", "rise"),
    "thickness": ("thickness", "thick", "gauge"),
    "diameter": ("diameter", "dia", "across"),
    "radius": ("radius",),
}

# Measurements that describe a *relationship between* parts, not the size of
# one. "Leg spacing reference: 800mm" is the distance between legs; counting it
# as a leg's own length made each leg eleven times too heavy.
REFERENCE_WORDS = (
    "spacing", "reference", "centre", "center", "between", "apart",
    "clearance", "gap", "offset", "pitch", "inset", "overhang",
    "from floor", "above floor", "adjustment", "travel", "range",
)


def to_mm(value: float, unit: str) -> float:
    return float(value) * TO_MM.get(unit, 1.0)


def measure(d: dict) -> float:
    try:
        return to_mm(float(d["value"]), d.get("unit", "mm"))
    except (TypeError, ValueError, KeyError):
        return 0.0


def is_reference(d: dict) -> bool:
    name = str(d.get("name", "")).lower()
    return any(word in name for word in REFERENCE_WORDS)


def own_dimensions(part: dict) -> list[dict]:
    """The part's own measurements, with relationship measurements removed."""
    return [d for d in part.get("dimensions", [])
            if measure(d) > 0 and not is_reference(d)]


def sorted_mm(part: dict) -> list[float]:
    return sorted((measure(d) for d in own_dimensions(part)), reverse=True)


def axis_of(name: str) -> str | None:
    """Which axis a measurement's name refers to.

    The axis whose word appears **earliest in the name** wins, rather than
    whichever the synonym table happens to list first. Names routinely mention
    more than one axis:

        "apron height (depth of the rail below the top)"  -> height
        "cross-section width and depth"                   -> width

    Walking the table in its own order gave the first of those a *depth* of
    90mm, so an apron that should have been 1200 x 25 x 90 was built
    1200 x 90 x 90 - a square beam instead of a board. The leading word is the
    dimension's actual name; anything after it is explanation.
    """
    lowered = str(name or "").lower()
    best: tuple[int, str] | None = None
    for axis, words in AXIS_SYNONYMS.items():
        for word in words:
            at = lowered.find(word)
            if at == -1:
                continue
            if best is None or at < best[0]:
                best = (at, axis)
    return best[1] if best else None


def axes(part: dict) -> dict[str, float]:
    """Map the part's measurements onto canonical axes, in millimetres.

    First value per axis wins; anything unmatched is kept as `_extra_n` so it
    can still fill a gap rather than being discarded.
    """
    out: dict[str, float] = {}
    extras = 0
    for d in own_dimensions(part):
        value = measure(d)
        axis = axis_of(d.get("name", ""))
        if axis is not None and axis not in out:
            out[axis] = value
        elif axis is None:
            out[f"_extra_{extras}"] = value
            extras += 1
    return out


# Primitives whose cross-section is a circle. For these a stated diameter is
# two sides of the envelope, not one.
ROUND_PRIMITIVES = frozenset({"cylinder", "tube", "revolve"})


def round_axis(pool: dict[str, float]) -> tuple[float, str] | None:
    """A round part's axial measurement, and which way that axis points.

    The name settles the direction, because the sizes cannot. A gas lift is
    "diameter 50, length 240" and stands up; a bar handle is "length 160,
    diameter 12" and lies across a drawer front. Both are a long thin cylinder
    described by a length, and only the word tells them apart - so `length`
    means the X axis, which is what a maker means by the length of a rail, and
    `height` and `thickness` mean Z.

    Returned rather than applied so `box_extent` and the builder can share one
    answer. They disagreed once already - a leg built 70x70x735 and costed as
    800x735x70 - and this module exists so that cannot happen again.
    """
    diameter = pool.get("diameter") or (pool.get("radius", 0.0) * 2)
    if not diameter:
        return None
    if pool.get("height"):
        return pool["height"], "z"
    if pool.get("length"):
        return pool["length"], "x"
    if pool.get("thickness"):
        return pool["thickness"], "z"
    # Nothing named the axis. A disc is the commoner shape, and the largest
    # leftover measurement is the best guess at its depth.
    others = sorted((v for k, v in pool.items()
                     if k not in ("diameter", "radius") and v > 0), reverse=True)
    return (others[0] if others else diameter), "z"


def _round_extent(pool: dict[str, float]) -> tuple[float, float, float] | None:
    """(x, y, z) for a round part: the diameter twice, and the axis once.

    Fed through the general rules a stool seat measured "diameter 340,
    thickness 45" came out 5 x 340 x 45 - a plank on edge rather than a disc -
    because the diameter was assigned to one horizontal axis and the other was
    filled from whatever was left over. Every round part in that model was
    wrong the same way: the legs, the glides and the mounting plate. A circle
    is as wide as it is deep; there is nothing to infer.
    """
    axial = round_axis(pool)
    if axial is None:
        return None
    length, axis = axial
    diameter = pool.get("diameter") or (pool.get("radius", 0.0) * 2)
    return (length, diameter, diameter) if axis == "x" else (diameter, diameter, length)


def box_extent(part: dict) -> tuple[float, float, float]:
    """Three sides for a box, from however the spec named them.

    Each axis is *consumed* when assigned, so a thickness cannot be used for
    the height and then reused for the depth - that produced beams where
    boards were intended.
    """
    # A round part's diameter is its width AND its depth.
    #
    # Fed through the general rules below, a stool seat measured "diameter 340,
    # thickness 45" came out 5 x 340 x 45 - a plank on edge rather than a disc,
    # because the diameter was assigned to one horizontal axis and the other
    # was filled from whatever was left over. Every round part in the model was
    # wrong the same way: the legs, the glides and the mounting plate.
    #
    # There is nothing to infer here. A circle is as wide as it is deep, so the
    # envelope of a round part is (diameter, diameter, length along the axis),
    # and the only question is which measurement is the axial one.
    if part.get("primitive") in ROUND_PRIMITIVES:
        round_extent = _round_extent(axes(part))
        if round_extent is not None:
            return round_extent

    pool = axes(part)
    extras = sorted((v for k, v in pool.items() if k.startswith("_extra")),
                    reverse=True)
    for key in [k for k in pool if k.startswith("_extra")]:
        pool.pop(key)

    def take(*names):
        for n in names:
            if n in pool:
                return pool.pop(n)
        return None

    # A part that names BOTH a width and a depth has already told us its
    # footprint, and any `length` it also carries is a third word for one of
    # them rather than a third side.
    #
    # Without this, `length` was taken for X first and the width was left in
    # the pool with nowhere to go. A seat labelled "width 43, length 46,
    # thickness 6" - which is exactly how the office chair drawing labels it -
    # was built 460 wide and 450 deep, so the 430 the drawing actually states
    # was read correctly, carried all the way through the specification, and
    # then dropped on the floor here. The seat came out rotated a quarter turn
    # and 30mm too wide, and nothing downstream could tell, because the number
    # that would have contradicted it had been discarded.
    #
    # `width` is side to side and `depth` is front to back by definition, so
    # when both are present they are the answer and no guess is needed.
    if "width" in pool and "depth" in pool:
        x = pool.pop("width")
        y = pool.pop("depth")
        z = take("height", "thickness")
    else:
        # `depth` is never a candidate for X. It means front-to-back, which is
        # Y by definition, and offering it to X first is how a cabinet side
        # panel - "height 645, depth 414, thickness 18" - came out 414mm wide
        # and 18mm deep: a panel lying across the carcass instead of standing
        # up its side. X takes the words that genuinely describe a side-to-side
        # extent, and the thickness falls through to whatever is left.
        x = take("length", "width")
        z = take("height", "thickness")
        y = take("depth", "width", "thickness", "diameter")

    remaining = sorted(pool.values(), reverse=True) + extras
    if x is None and remaining:
        x = remaining.pop(0)
    if y is None and remaining:
        y = remaining.pop(0)
    if z is None and remaining:
        z = remaining.pop(0)

    known = [v for v in (x, y, z) if v] or [100.0]
    smallest = min(known)
    return (x or smallest, y or smallest, z or smallest)


def cylinder_extent(part: dict) -> tuple[float, float]:
    """(diameter, length) in millimetres.

    Defers to `round_axis` whenever a diameter is stated, so this and
    `box_extent` cannot give different answers about the same part. They did:
    a bicycle tyre measured "diameter 678, width 28" was an envelope 678 x 678
    x 28 here and a cylinder 678 across and 678 LONG there, because `width` was
    not among the words this function would accept as an axial length. Both
    wheels came out as one enormous drum.
    """
    a = axes(part)
    axial = round_axis(a)
    if axial is not None:
        return (a.get("diameter") or (a.get("radius", 0) * 2)), axial[0]

    # No diameter anywhere: fall back to reading it off the sizes themselves.
    sizes = sorted_mm(part)
    diameter = a.get("width") or (sizes[1] if len(sizes) > 1 else 50.0)
    length = (a.get("height") or a.get("length") or a.get("thickness")
              or (sizes[0] if sizes else diameter))
    return diameter, length


def sweep_section(part: dict) -> tuple[float, float, bool]:
    """A swept part's section as (width, thickness, is_round)."""
    a = axes(part)
    diameter = a.get("diameter") or (a.get("radius", 0) * 2)
    if diameter:
        return diameter, diameter, True
    sides = sorted(box_extent(part))
    return sides[1], sides[0], False


def path_length_mm(part: dict) -> float:
    """How long a swept part's centre line is, in millimetres.

    This is the length a maker cuts, and it is nothing like the part's
    bounding box: an armrest loop 260mm long and 190mm tall is bent from
    roughly 700mm of tube. Costing it by its envelope would order a third of
    the material needed, which is exactly the sort of quiet disagreement
    between the model and the bill that sharing this module exists to prevent.

    Measured along the straight segments between the stated points. The built
    curve is smoothed through them, so the true length is a little greater -
    erring short on a cutting list would be the wrong direction, but the
    difference is under a percent for the handful of points a path carries,
    and a smoothed curve is never shorter than its polyline.
    """
    from forge.spec import schema  # noqa: PLC0415

    shape = schema.shape_of(part)
    path = shape["path"]
    if len(path) < 2:
        return 0.0

    plane = shape["path_plane"]
    u_axis, v_axis = {"xz": (0, 2), "yz": (1, 2), "xy": (0, 1)}.get(plane, (0, 2))
    extent = box_extent(part)
    u_size, v_size = extent[u_axis], extent[v_axis]

    points = [(p["u"] * u_size, p["v"] * v_size) for p in path]
    if shape["path_closed"]:
        points.append(points[0])

    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def volume_mm3(part: dict) -> float:
    """Volume of ONE instance, in cubic millimetres."""
    primitive = part.get("primitive", "box")

    if primitive == "sweep":
        length = path_length_mm(part)
        if length <= 0:
            # No path: it was built as a box, so it is costed as one.
            x, y, z = box_extent(part)
            return x * y * z
        width, thickness, round_section = sweep_section(part)
        area = (math.pi * (width / 2) ** 2 if round_section
                else width * thickness)
        return area * length

    if primitive in ("cylinder", "revolve"):
        diameter, length = cylinder_extent(part)
        return math.pi * (diameter / 2) ** 2 * length

    if primitive == "tube":
        diameter, length = cylinder_extent(part)
        wall = axes(part).get("thickness") or max(1.5, diameter * 0.06)
        outer = diameter / 2
        inner = max(0.0, outer - wall)
        return math.pi * (outer ** 2 - inner ** 2) * length

    if primitive == "sphere":
        a = axes(part)
        diameter = a.get("diameter") or (a.get("radius", 25) * 2)
        return (4 / 3) * math.pi * (diameter / 2) ** 3

    x, y, z = box_extent(part)
    volume = x * y * z
    if primitive == "cushion":
        volume *= 0.92       # a bevelled box is less than its bounding box
    return volume


def surface_mm2(part: dict) -> float:
    """Outer surface area of ONE instance, in square millimetres."""
    primitive = part.get("primitive", "box")

    if primitive == "sweep":
        length = path_length_mm(part)
        if length > 0:
            width, thickness, round_section = sweep_section(part)
            perimeter = (math.pi * width if round_section
                         else 2 * (width + thickness))
            return perimeter * length

    if primitive in ("cylinder", "tube", "revolve"):
        diameter, length = cylinder_extent(part)
        r = diameter / 2
        return 2 * math.pi * r * length + 2 * math.pi * r ** 2

    if primitive == "sphere":
        a = axes(part)
        diameter = a.get("diameter") or (a.get("radius", 25) * 2)
        return 4 * math.pi * (diameter / 2) ** 2

    x, y, z = box_extent(part)
    return 2 * (x * y + y * z + x * z)


def longest_mm(part: dict) -> float:
    """The dimension a piece would be cut to length on.

    For a bent part that is the length of the curve, not the size of the box
    it ends up occupying - you cut the tube straight and bend it afterwards.
    """
    if part.get("primitive") == "sweep":
        length = path_length_mm(part)
        if length > 0:
            return length
    return max(box_extent(part))
