"""Fasteners, derived from how the parts are joined.

This is the part of the bill people actually check, and the part most likely
to be invented. It is computed here instead:

  joint type  ×  how many times it occurs  ×  the thickness of the material
  being joined  =  a count of a specific fastener size

The size comes from a rule makers already use: **a screw should be about two
and a half times the thickness of the part it passes through**, so it holds in
the second member without bursting through it. Feed in 18mm sheet and you get
40mm screws; feed in 45mm timber and you get 80mm. Change a thickness in the
spec and the size changes with it, which is the whole reason this is a
calculation and not a lookup table.
"""

from __future__ import annotations

import math

from ..spec import dims as spec_dims

# Available screw and nail lengths, in millimetres. A computed ideal length is
# rounded to the nearest real size - you cannot buy a 47mm screw.
SCREW_SIZES = [25, 30, 40, 50, 65, 80]
NAIL_SIZES = [25, 50, 75]




def thickness_of(part: dict) -> float:
    """The thinnest meaningful dimension of a part, in millimetres.

    A fastener passes through the *thin* direction - the 18mm face of a shelf,
    not its 600mm length - so the smallest dimension is the one that sets the
    size.
    """
    # Reference measurements are excluded via the shared resolver: a "ground
    # clearance: 80mm" entry is not the thickness of anything, and sizing a
    # screw for it would ask for an 80mm screw into a 20mm apron.
    values = spec_dims.sorted_mm(part)
    if not values:
        return 18.0
    return min(values)


def _nearest(target: float, sizes: list[int]) -> int:
    return min(sizes, key=lambda s: abs(s - target))


def screw_for(thickness: float) -> int:
    """The screw length for a given material thickness."""
    return _nearest(thickness * 2.5, SCREW_SIZES)


def nail_for(thickness: float) -> int:
    return _nearest(thickness * 2.5, NAIL_SIZES)


# For each joint type: how many fasteners of what kind, per occurrence.
#
# `per` is the count of fasteners for one instance of that joint. The numbers
# are ordinary workshop practice: two screws into an apron end so it cannot
# pivot, four bolts on a knock-down fixing, two dowels in a rail.
RULES = {
    "butt_screwed":  {"kind": "screw", "per": 2, "glue_ml": 4},
    "corner_block":  {"kind": "screw", "per": 4, "extra": ("bracket_corner", 1),
                      "glue_ml": 6},
    "pocket_screw":  {"kind": "pocket_screw", "per": 2, "glue_ml": 4},
    "dowelled":      {"kind": "dowel", "per": 2, "glue_ml": 8},
    "mortise_tenon": {"kind": None, "per": 0, "glue_ml": 14},
    "bolted":        {"kind": "bolt", "per": 2},
    "nailed":        {"kind": "nail", "per": 4},
    "stapled":       {"kind": "staple", "per": 0, "per_100mm": 7},
    "welded":        {"kind": None, "per": 0},
    "glued":         {"kind": None, "per": 0, "glue_ml": 12},
    "bracket":       {"kind": "screw", "per": 4, "extra": ("bracket_corner", 1)},
    "press_fit":     {"kind": None, "per": 0},
}

JOINT_LABEL = {
    "butt_screwed": "Butt joint, screwed",
    "corner_block": "Corner block",
    "pocket_screw": "Pocket-hole screws",
    "dowelled": "Dowelled",
    "mortise_tenon": "Mortise and tenon",
    "bolted": "Bolted",
    "nailed": "Nailed",
    "stapled": "Stapled",
    "welded": "Welded",
    "glued": "Glued only",
    "bracket": "Steel bracket",
    "press_fit": "Press fit",
}


def perimeter_mm(part: dict) -> float:
    """Rough perimeter of a part, for joints counted per unit length."""
    values = spec_dims.sorted_mm(part)
    if len(values) >= 2:
        return 2 * (values[0] + values[1])
    return values[0] * 4 if values else 400.0


def schedule(spec: dict) -> tuple[dict[str, dict], list[str]]:
    """Return ({fastener_code: {qty, description}}, notes).

    Notes record anything inferred rather than read, so a bill can say which
    of its numbers rest on an assumption.
    """
    parts = {p["id"]: p for p in spec.get("parts", [])}
    joints = spec.get("joints", []) or []
    tally: dict[str, dict] = {}
    notes: list[str] = []

    def add(code: str, qty: float, description: str, because: str):
        entry = tally.setdefault(code, {"qty": 0.0, "description": description,
                                        "because": []})
        entry["qty"] += qty
        if because not in entry["because"]:
            entry["because"].append(because)

    if not joints:
        notes.append(
            "The specification lists no joints, so no fasteners could be "
            "calculated. Re-run the analysis to have them identified."
        )
        return tally, notes

    for joint in joints:
        rule = RULES.get(joint.get("type", ""), RULES["butt_screwed"])
        occurrences = max(1, int(joint.get("count", 1)))
        label = JOINT_LABEL.get(joint.get("type", ""), joint.get("type", "joint"))

        # The thinner of the joined parts decides the fastener size: a screw
        # sized for the thick member would burst through the thin one.
        joined = [parts[pid] for pid in joint.get("parts", []) if pid in parts]
        if joined:
            thickness = min(thickness_of(p) for p in joined)
        else:
            thickness = 18.0
            notes.append(
                f"Joint “{joint.get('id')}” names parts that are not in the "
                f"specification; assumed 18mm material."
            )

        because = f"{occurrences} × {label}"

        kind = rule.get("kind")
        if kind == "screw":
            size = screw_for(thickness)
            add(f"screw_{size}", rule["per"] * occurrences,
                f"Wood screw, {size}mm", because)
        elif kind == "pocket_screw":
            add("pocket_screw_32", rule["per"] * occurrences,
                "Pocket-hole screw, 32mm", because)
        elif kind == "nail":
            size = nail_for(thickness)
            add(f"nail_{size}", rule["per"] * occurrences,
                f"Nail, {size}mm", because)
        elif kind == "dowel":
            add("dowel_8x40", rule["per"] * occurrences,
                "Beech dowel, 8 × 40mm", because)
        elif kind == "bolt":
            add("bolt_m8x60", rule["per"] * occurrences,
                "Bolt M8 × 60mm with nut and washer", because)
        elif kind == "staple":
            run = sum(perimeter_mm(p) for p in joined) or 400.0
            count = math.ceil(run / 100.0) * rule.get("per_100mm", 7) * occurrences
            add("staple_10", count, "Upholstery staple, 10mm", because)

        extra = rule.get("extra")
        if extra:
            code, per = extra
            add(code, per * occurrences,
                "Corner bracket, steel" if code == "bracket_corner" else code,
                because)

        glue = rule.get("glue_ml")
        if glue:
            add("wood_glue_ml", glue * occurrences, "PVA wood glue", because)

    # Round up: you buy whole screws.
    for entry in tally.values():
        entry["qty"] = math.ceil(entry["qty"])

    return tally, notes
