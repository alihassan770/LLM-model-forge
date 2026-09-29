"""Deterministic checks on an assembled model, before anyone opens the file.

Everything here is arithmetic on the specification and its placements. No model
is asked, nothing is rendered, and it costs nothing - which is the point. The
faults it looks for are the ones that have actually shipped:

* a sideboard assembled 127% too deep, because an internal width was mistaken
  for the object's depth;
* a chair base a fifth too small, because an anchor named an arm's centre and
  the arms had to reach a stated diameter;
* panels sitting a metre from the object they belong to.

Every one of those is obvious the moment the assembled size is compared with
the size the specification itself states - a comparison nothing was doing. The
specification carried the right number and the geometry disagreed with it, and
nobody found out until a person opened the file and saw a heap of boards.

The checks are chosen for signal rather than coverage. A furniture model has
parts that legitimately overlap (a tenon in its mortise, a cushion on its
shell), legitimately cantilever, and legitimately overhang, so tests for those
would cry wolf until they were ignored. What is never legitimate is a part
touching nothing at all, a part underneath the floor, or an object twice the
size it says it is.
"""

from __future__ import annotations

import math

from forge.build import layout
from forge.spec import dims as spec_dims

# --------------------------------------------------------------------------- #
# Tolerances
# --------------------------------------------------------------------------- #

# How far the assembled object may differ from its stated size before it is
# called wrong. Deliberately loose: a stated height rarely includes a castor or
# a levelling foot, and a top legitimately overhangs its frame. Measured
# against real extractions, a correct table came in at 7% and a correct chair
# at 3%, while the broken sideboard was 39% and 127% - so anything above 15%
# is a fault rather than a detail.
ENVELOPE_TOLERANCE = 0.15

# A part may reach this far outside the stated envelope before it is called
# stray, as a fraction of the object plus a flat allowance. An overhanging top
# or a projecting handle must not trip this; a panel in the row beside the
# object must.
STRAY_FRACTION = 0.25
STRAY_ALLOWANCE_MM = 60.0

# Two parts count as touching if their boxes come within this of each other.
# Generous, because a specification's dimensions are rounded and a joint is
# often described with a millimetre of daylight in it.
CONTACT_TOLERANCE_MM = 12.0

# How far below the floor a part may reach. Castors and glides are modelled
# with their own thickness and a rounding error should not be a fault.
BELOW_FLOOR_TOLERANCE_MM = 5.0

# What each primitive needs before it has a shape at all. The prompt asks for
# this; nothing until now checked that it arrived.
REQUIRED_DIMENSIONS = {
    "box": 3, "cushion": 3, "extrude": 3,
    "cylinder": 2, "tube": 2, "revolve": 2,
    "sphere": 1,
    # A sweep needs an envelope to scale its curve into and a section to
    # carry along it - the same three numbers a box needs.
    "sweep": 3,
}


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def _rotation_matrix(rx: float, ry: float, rz: float) -> list[list[float]]:
    """Blender's default XYZ euler order, in degrees: R = Rz · Ry · Rx."""
    x, y, z = math.radians(rx), math.radians(ry), math.radians(rz)
    cx, sx = math.cos(x), math.sin(x)
    cy, sy = math.cos(y), math.sin(y)
    cz, sz = math.cos(z), math.sin(z)
    return [
        [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
        [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
        [-sy,     cy * sx,                cy * cx],
    ]


def aabb(position: tuple, rotation: tuple, extent: tuple
         ) -> tuple[list[float], list[float]]:
    """The world-aligned box a rotated part occupies.

    Computed from the eight corners rather than by swapping axes, so a part
    leaning fifteen degrees is measured as it actually sits rather than as the
    nearest right angle.
    """
    matrix = _rotation_matrix(*rotation)
    half = [extent[0] / 2.0, extent[1] / 2.0, extent[2] / 2.0]

    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for sx in (-1, 1):
        for sy in (-1, 1):
            for sz in (-1, 1):
                corner = (half[0] * sx, half[1] * sy, half[2] * sz)
                for axis in range(3):
                    value = position[axis] + sum(
                        matrix[axis][k] * corner[k] for k in range(3)
                    )
                    lo[axis] = min(lo[axis], value)
                    hi[axis] = max(hi[axis], value)
    return lo, hi


def _boxes_touch(a: tuple, b: tuple, tolerance: float) -> bool:
    (a_lo, a_hi), (b_lo, b_hi) = a, b
    return all(a_lo[i] - tolerance <= b_hi[i] and b_lo[i] - tolerance <= a_hi[i]
               for i in range(3))


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #

def _finding(kind: str, severity: str, detail: str, part_id: str = "") -> dict:
    return {"kind": kind, "severity": severity, "part_id": part_id,
            "detail": detail}


def _assembled_boxes(spec: dict, placements: dict) -> dict:
    """{(part_id, index): (lo, hi)} for everything that got placed."""
    boxes = {}
    for part in spec.get("parts", []):
        extent = spec_dims.box_extent(part)
        for index in range(max(1, int(part.get("count", 1) or 1))):
            pose = placements.get((part["id"], index))
            if pose is None:
                continue
            position, rotation = pose
            boxes[(part["id"], index)] = aabb(position, rotation, extent)
    return boxes


def _check_dimensions(spec: dict) -> list[dict]:
    """Does every part carry enough measurements to have a shape?"""
    out = []
    for part in spec.get("parts", []):
        primitive = part.get("primitive", "box")
        needed = REQUIRED_DIMENSIONS.get(primitive)
        if needed is None:
            continue
        have = len(spec_dims.own_dimensions(part))
        if have < needed:
            out.append(_finding(
                "under_specified", "error",
                f"“{part.get('display_name') or part['id']}” is a {primitive} "
                f"with {have} usable dimension(s); it needs {needed} to have a "
                f"shape, so its size is being guessed.",
                part["id"]))
    return out


def _check_envelope(boxes: dict, stated: dict) -> list[dict]:
    """Is the assembled object the size the specification says it is?

    The single most valuable check here. A specification that states 1800 x 450
    and assembles to 2498 x 1022 is not a matter of taste - one of the two is
    wrong, and either way the file should not go out silently.
    """
    if not boxes:
        return []

    lo = [min(b[0][i] for b in boxes.values()) for i in range(3)]
    hi = [max(b[1][i] for b in boxes.values()) for i in range(3)]
    built = [hi[i] - lo[i] for i in range(3)]
    want = [stated.get("x_span", 0.0), stated.get("y_span", 0.0),
            stated.get("height", 0.0)]

    out = []
    for axis, name in enumerate(("width", "depth", "height")):
        if want[axis] <= 0:
            continue
        error = abs(built[axis] - want[axis]) / want[axis]
        if error > ENVELOPE_TOLERANCE:
            out.append(_finding(
                "envelope_mismatch", "error",
                f"The specification states a {name} of {want[axis]:.0f}mm, but "
                f"the parts assemble to {built[axis]:.0f}mm - "
                f"{error * 100:.0f}% out. Either a measurement is being read "
                f"onto the wrong axis, or parts are not where they belong."))
    return out


def _check_strays(boxes: dict, stated: dict) -> list[dict]:
    """Which parts are sitting outside the object they belong to.

    The actionable companion to the envelope check: that one says the model is
    the wrong size, this one says which pieces to look at.
    """
    x_span = stated.get("x_span", 0.0)
    y_span = stated.get("y_span", 0.0)
    if x_span <= 0 or y_span <= 0:
        return []

    limit_x = x_span / 2.0 * (1 + STRAY_FRACTION) + STRAY_ALLOWANCE_MM
    limit_y = y_span / 2.0 * (1 + STRAY_FRACTION) + STRAY_ALLOWANCE_MM

    seen, out = set(), []
    for (part_id, _), (lo, hi) in sorted(boxes.items()):
        if part_id in seen:
            continue
        over_x = max(abs(lo[0]), abs(hi[0])) - limit_x
        over_y = max(abs(lo[1]), abs(hi[1])) - limit_y
        if over_x > 0 or over_y > 0:
            seen.add(part_id)
            out.append(_finding(
                "stray_part", "warning",
                f"“{part_id}” sits up to {max(over_x, over_y):.0f}mm outside "
                f"the object's stated footprint. Parts no rule could place are "
                f"laid out beside the model on purpose, so this is either that "
                f"or a placement that went wrong.",
                part_id))
    return out


def _check_detached(boxes: dict) -> list[dict]:
    """Parts touching nothing else in the model.

    Chosen over a test for floating or unsupported parts because furniture is
    full of things that hang, cantilever and fix sideways, and only a part that
    touches *nothing* is unambiguously wrong. A single object is exempt: a
    one-part model is not detached from anything.
    """
    if len(boxes) < 2:
        return []

    keys = list(boxes)
    seen, out = set(), []
    for i, key in enumerate(keys):
        part_id = key[0]
        if part_id in seen:
            continue
        touching = any(
            _boxes_touch(boxes[key], boxes[other], CONTACT_TOLERANCE_MM)
            for j, other in enumerate(keys) if i != j and other[0] != part_id
        )
        if not touching:
            seen.add(part_id)
            out.append(_finding(
                "detached", "warning",
                f"“{part_id}” does not touch any other part. A piece of "
                f"furniture is joined to something; this one is floating on "
                f"its own.",
                part_id))
    return out


def _check_below_floor(boxes: dict) -> list[dict]:
    """Anything sunk through the ground it is supposed to stand on."""
    seen, out = set(), []
    for (part_id, _), (lo, _hi) in sorted(boxes.items()):
        if part_id in seen or lo[2] >= -BELOW_FLOOR_TOLERANCE_MM:
            continue
        seen.add(part_id)
        out.append(_finding(
            "below_floor", "warning",
            f"“{part_id}” reaches {abs(lo[2]):.0f}mm below the floor. This is "
            f"usually an alignment that should have been “min” - the face that "
            f"rests on something - rather than “center”.",
            part_id))
    return out


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def inspect(spec: dict, placements: dict | None = None,
            swapped_plan: bool = False) -> list[dict]:
    """Every fault worth reporting about this model, worst first.

    `placements` is optional so this can be run on a stored specification
    without a build; it is passed in by the builder, which has already computed
    them and should not pay to do it twice.

    `swapped_plan` must match how the placements were made. An assembly built
    with its length on Y has to be measured against a stated size with its
    length on Y, or the check marks a correct model wrong: a bicycle assembled
    exactly 1713mm long was reported as 289% too deep, because its length was
    being compared against the 440mm handlebar width. That mismeasurement was
    the only evidence for concluding the swapped reading never helps, so it
    threw away the answer it was asked to find.
    """
    if not spec.get("parts"):
        return [_finding("no_parts", "error",
                         "The specification lists no parts, so there is "
                         "nothing to build.")]

    if placements is None:
        placements = layout.place(spec, swap_plan=swapped_plan)

    stated = dict(layout._overall(spec))
    if swapped_plan:
        stated["x_span"], stated["y_span"] = stated["y_span"], stated["x_span"]
    boxes = _assembled_boxes(spec, placements)

    findings = (
        _check_dimensions(spec)
        + _check_envelope(boxes, stated)
        + _check_strays(boxes, stated)
        + _check_detached(boxes)
        + _check_below_floor(boxes)
    )
    # Errors first, then warnings, each in the order they were found - so the
    # thing most likely to have ruined the model is the first line read.
    rank = {"error": 0, "warning": 1}
    findings.sort(key=lambda f: rank.get(f["severity"], 2))
    return findings


def score(findings: list[dict]) -> int:
    """How wrong a model is, lower being better.

    An error counts for three because the things reported as errors are the
    ones that make a file useless - an object twice the size it says it is -
    while a warning is usually one piece in the wrong place.
    """
    return sum(3 if f.get("severity") == "error" else 1 for f in findings)


def best_placements(spec: dict) -> tuple[str, dict, list[dict]]:
    """Assemble the object both ways and keep whichever measures better.

    There are two ways to place a specification's parts: by the naming
    conventions in `layout`, or by the positions the model reported. Choosing
    between them used to be a threshold - trust the model once it has placed
    70% of the parts - and that number was picked by hand with no way to tell
    whether it was right. The Oakley sideboard landed at 68% and took the
    conventions, which assembled it 127% too deep.

    Now both are built and measured against the dimensions the specification
    itself states, and the better one wins. Placement is pure arithmetic - no
    Blender, no model, microseconds - so trying both costs nothing worth
    counting.

    Ties go to the conventions, which are the older and better-tested path.
    """
    from forge.build import layout  # noqa: PLC0415

    automatic = layout.place(spec)
    findings = inspect(spec, automatic)
    best = ("automatic", automatic, findings)
    if not findings:
        return best

    described = any(part.get("placement") for part in spec.get("parts", []))

    # Each candidate is a different reading of the same specification. They
    # cost microseconds, so rather than argue about which reading is right the
    # object is built every way and measured against its own stated size.
    #
    # `swapped` puts the object's length on Y. A table's long axis is
    # side-to-side and a bicycle's is front-to-back, and no measurement
    # distinguishes them - but the model says which it means, by placing a
    # bike's wheels at anchor_y 0.2 and 0.8, `mirror_y`. That is correct
    # English, the front wheel is at the front, and it only lands them a
    # wheelbase apart if the length is on Y.
    candidates = [("swapped", {"swap_plan": True})]
    if described:
        candidates.insert(0, ("model-placed", {"trust_placement": True}))
        candidates.append(("model-placed swapped",
                           {"trust_placement": True, "swap_plan": True}))

    for label, options in candidates:
        try:
            placements = layout.place(spec, **options)
        except Exception:  # noqa: BLE001
            continue
        # Measured against the plan it was built with, not the other one.
        other = inspect(spec, placements,
                        swapped_plan=options.get("swap_plan", False))
        if score(other) < score(best[2]):
            best = (label, placements, other)
    return best


def summarise(findings: list[dict]) -> str:
    """One line for a log or a job row."""
    if not findings:
        return "geometry checks passed"
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = len(findings) - errors
    parts = []
    if errors:
        parts.append(f"{errors} error(s)")
    if warnings:
        parts.append(f"{warnings} warning(s)")
    return "geometry checks: " + ", ".join(parts)
