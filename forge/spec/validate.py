"""The completeness gate.

Deterministic Python, not a model call, and the single point that decides
whether a specification may be built. Everything it checks is something that
would otherwise fail inside Blender - or worse, succeed and produce a wrong
object - with nothing useful to show the user.

Problems are returned naming the part and the fix. "Validation failed" tells
nobody anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import schema as spec_schema

# How many independent positive dimensions each primitive needs before geometry
# can be generated at all.
REQUIRED_DIMENSION_COUNT = {
    "box": 3,
    "cushion": 3,
    "cylinder": 2,      # a diameter and a length
    "tube": 2,
    "sphere": 1,
    "revolve": 2,
    "extrude": 2,
}

# Roles where a missing dimension is a warning rather than a refusal. What
# separates them is whether a wrong guess shows: nobody measures the thickness
# of a hinge leaf, and nobody will notice it being a millimetre out, but a
# panel or a tabletop with no thickness is a real hole in the design. Applied
# only when at least one dimension is known - a part with no size at all is
# not a part.
FORGIVING_ROLES = {"hardware", "decorative"}

# Names that mean "across", so a box described as width/depth/height and one
# described as length/width/thickness both resolve to three distinct axes.
AXIS_WORDS = {
    "length", "len", "width", "depth", "height", "thickness", "radius",
    "diameter", "size", "section", "span", "rise",
}


@dataclass
class Report:
    ok: bool
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    open_questions: int = 0
    buildable_parts: int = 0
    total_parts: int = 0


def _positive_dimensions(part: dict) -> list[dict]:
    return [d for d in part.get("dimensions", [])
            if isinstance(d.get("value"), (int, float)) and d["value"] > 0]


def _distinct_axes(dims: list[dict]) -> int:
    """Count dimensions that describe different things.

    'section width' and 'width' are the same axis stated twice; counting both
    would let a part pass the gate with one real measurement.
    """
    seen = set()
    for d in dims:
        words = {w for w in d.get("name", "").lower().replace("-", " ").split()
                 if w in AXIS_WORDS}
        key = tuple(sorted(words)) or (d.get("name", "").lower(),)
        seen.add(key)
    return len(seen)


def check(spec: dict) -> Report:
    problems: list[str] = []
    warnings: list[str] = []

    parts = spec.get("parts", [])
    if not parts:
        return Report(False, ["No parts were identified, so there is nothing to build."])

    ids = {p["id"] for p in parts}
    buildable = 0

    for part in parts:
        name = part.get("display_name") or part["id"]
        dims = _positive_dimensions(part)

        for d in part.get("dimensions", []):
            value = d.get("value")
            if not isinstance(value, (int, float)) or value <= 0:
                problems.append(
                    f"“{name}” has a {d.get('name', 'dimension')} of {value}, "
                    f"which cannot be built."
                )

        needed = REQUIRED_DIMENSION_COUNT.get(part.get("primitive", "box"), 2)
        have = _distinct_axes(dims)
        if have >= needed:
            buildable += 1
        elif have and part.get("role") in FORGIVING_ROLES:
            # A hinge leaf 3mm instead of 2mm changes nothing anyone will
            # notice, and `dims.box_extent` already substitutes the smallest
            # known side for a missing one. Blocking the build over it means a
            # bracket costs the user all nineteen other parts - so this is a
            # warning, and the substitution is stated rather than hidden.
            warnings.append(
                f"“{name}” gives {have} of the {needed} dimensions a "
                f"{part.get('primitive')} needs; the missing one is taken as "
                f"its smallest known side. Fine for hardware, worth checking "
                f"if it matters to you."
            )
            buildable += 1
        else:
            problems.append(
                f"“{name}” is a {part.get('primitive')} but has only {have} "
                f"of the {needed} dimensions it needs."
            )

        if part.get("parent_id") and part["parent_id"] not in ids:
            problems.append(
                f"“{name}” belongs to a part that does not exist "
                f"({part['parent_id']})."
            )

        if part.get("count", 1) < 1:
            problems.append(f"“{name}” has a quantity below one.")

        if not part.get("material") or part["material"] == "unspecified":
            warnings.append(f"“{name}” has no material, so it cannot be costed.")

        # A part thinner than a tenth of a millimetre is a unit mistake, not a
        # design. Caught here because Blender will build it happily.
        for d in dims:
            mm = spec_schema.measurement_mm(d)
            if mm is not None and 0 < mm < 0.1:
                warnings.append(
                    f"“{name}” has a {d['name']} of {mm:.3f}mm - check the units."
                )

    blocking = spec_schema.blocking_questions(spec)
    for q in blocking:
        problems.append(
            f"Unanswered: {q.get('question') or q.get('dimension')}"
        )

    low_confidence = [
        (p, d) for p in parts for d in _positive_dimensions(p)
        if d.get("confidence", 1) < 0.5 and p.get("role") == "structural"
    ]
    for part, d in low_confidence:
        warnings.append(
            f"“{part.get('display_name')}” - {d['name']} is a low-confidence "
            f"guess ({d['value']}{d['unit']}). Worth checking before building."
        )

    return Report(
        ok=not problems,
        problems=problems,
        warnings=warnings,
        open_questions=len(blocking),
        buildable_parts=buildable,
        total_parts=len(parts),
    )
