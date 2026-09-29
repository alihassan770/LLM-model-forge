"""Quantity takeoff: what material the object needs, and in what sizes.

Quantities and sizes only - no pricing. A price list belongs to a supplier and
goes stale; a cutting list does not.

Computed from the same specification the geometry is built from, which is what
guarantees the bill and the model cannot disagree. Change a leg height and both
change together; there is no second source of truth to fall out of step.

Every number here is arithmetic on dimensions. The model contributes no
quantities at all - it is asked afterwards only which grade and size to buy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..spec import dims as spec_dims
from ..spec import schema as spec_schema
from . import catalog, fasteners

CATEGORY_FOR_KIND = {
    "linear": "timber",
    "sheet": "sheet",
    "volume": "volume",
    "area": "fabric",
    "unit": "hardware",
}


# All of these delegate to forge/spec/dims.py, which the geometry builder also
# uses. They each had their own copy before and disagreed: a leg was built
# 70x70x735mm and costed as 800x735x70mm, because the takeoff treated a
# "leg spacing reference: 800mm" entry as one of the leg's own sides. Sharing
# the function is what makes "the bill cannot disagree with the model" a
# structural property rather than a promise.
_mm = spec_dims.measure
_sorted_dims = spec_dims.sorted_mm
_named = spec_dims.axes
volume_mm3 = spec_dims.volume_mm3
surface_mm2 = spec_dims.surface_mm2
longest_mm = spec_dims.longest_mm


# --------------------------------------------------------------------------- #
# Cutting list
# --------------------------------------------------------------------------- #

@dataclass
class CutPlan:
    stock_length: float
    pieces: list[float]
    sticks: int
    offcuts: list[float]
    yield_pct: float


def plan_cuts(lengths: list[float], stock_length: float,
              kerf: float = 3.0) -> CutPlan:
    """First-fit-decreasing bin packing: how many lengths of stock to buy.

    Longest pieces placed first, which is what a person does at the saw and
    what gives the best yield for almost no effort. The kerf is the material
    the blade removes - ignoring it is how a cutting list comes up one piece
    short on the last stick.
    """
    usable = sorted((length for length in lengths if 0 < length <= stock_length),
                    reverse=True)
    oversize = [length for length in lengths if length > stock_length]

    bins: list[float] = []   # remaining space in each stick
    for piece in usable:
        need = piece + kerf
        for i, remaining in enumerate(bins):
            if remaining >= need:
                bins[i] = remaining - need
                break
        else:
            bins.append(stock_length - need)

    # A piece longer than the stock has to be joined; counted as its own stick
    # and flagged, rather than silently dropped.
    for piece in oversize:
        bins.append(max(0.0, stock_length - (piece % stock_length)))

    total_stock = len(bins) * stock_length
    used = sum(lengths)
    return CutPlan(
        stock_length=stock_length,
        pieces=sorted(lengths, reverse=True),
        sticks=len(bins),
        offcuts=[round(b) for b in bins if b > 50],
        yield_pct=round((used / total_stock * 100) if total_stock else 0, 1),
    )


# --------------------------------------------------------------------------- #
# The takeoff
# --------------------------------------------------------------------------- #

@dataclass
class Line:
    category: str
    material_code: str
    description: str
    part_ids: list[str]
    quantity: float
    unit: str
    detail: dict = field(default_factory=dict)
    waste_pct: float = 0.0


@dataclass
class Bill:
    lines: list[Line]
    notes: list[str]
    total_weight_kg: float | None


ORDER = {"timber": 0, "sheet": 1, "volume": 2, "fabric": 3,
         "hardware": 4, "fastener": 5}


def compute(spec: dict, org_id: str) -> Bill:
    cat = catalog.for_org(org_id)
    parts = spec.get("parts", [])
    notes: list[str] = []
    lines: list[Line] = []
    weight_kg = 0.0

    # --- group parts by the catalogue entry they resolve to ------------------
    grouped: dict[str, list[dict]] = {}
    for part in parts:
        material = catalog.resolve(cat, part.get("material", ""))
        if material["code"] == "unspecified" and part.get("material"):
            notes.append(
                f"“{part.get('display_name', part['id'])}” is specified as "
                f"“{part['material']}”, which is not in the catalogue - it is "
                f"listed but not costed."
            )
        grouped.setdefault(material["code"], []).append(part)

    for code, group in grouped.items():
        material = cat.get(code) or catalog.resolve(cat, code)
        kind = material["kind"]
        category = CATEGORY_FOR_KIND.get(kind, "volume")
        waste = float(material.get("waste_pct") or 0)
        ids = [p["id"] for p in group]

        # ---- linear stock: a real cutting list ----------------------------
        if kind == "linear":
            stock = float(material.get("stock_length") or 2400)
            pieces: list[float] = []
            for part in group:
                pieces.extend([longest_mm(part)] * max(1, int(part.get("count", 1))))

            plan = plan_cuts(pieces, stock)
            total_mm = sum(pieces)
            lines.append(Line(
                category=category, material_code=code,
                description=f"{material['name']} - {plan.sticks} × {stock:.0f}mm",
                part_ids=ids, quantity=plan.sticks, unit="lengths",
                detail={
                    "pieces": [round(p) for p in plan.pieces],
                    "stock_length_mm": stock,
                    "total_required_mm": round(total_mm),
                    "yield_pct": plan.yield_pct,
                    "offcuts_mm": plan.offcuts,
                    "section": (f"{material.get('stock_width')}×"
                                f"{material.get('stock_height')}mm"),
                },
                waste_pct=round(100 - plan.yield_pct, 1),
            ))
            if plan.yield_pct < 60:
                notes.append(
                    f"{material['name']} is only {plan.yield_pct}% used - the "
                    f"offcuts may suit another part, or a shorter stock length."
                )

        # ---- sheet goods: area, then whole sheets --------------------------
        elif kind == "sheet":
            area_mm2 = 0.0
            for part in group:
                dims = _sorted_dims(part)
                face = (dims[0] * dims[1]) if len(dims) > 1 else 0.0
                area_mm2 += face * max(1, int(part.get("count", 1)))

            sheet_w = float(material.get("stock_width") or 2440)
            sheet_area = sheet_w * 1220
            with_waste = area_mm2 * (1 + waste / 100)
            sheets = math.ceil(with_waste / sheet_area) if sheet_area else 0
            lines.append(Line(
                category=category, material_code=code,
                description=f"{material['name']} - {sheets} sheet(s)",
                part_ids=ids, quantity=sheets, unit="sheets",
                detail={"area_m2": round(area_mm2 / 1e6, 3),
                        "sheet_size_mm": f"{sheet_w:.0f}×1220",
                        "with_waste_m2": round(with_waste / 1e6, 3)},
                waste_pct=waste,
            ))

        # ---- fabric: running metres off the roll ---------------------------
        elif kind == "area":
            area_mm2 = sum(surface_mm2(p) * max(1, int(p.get("count", 1)))
                           for p in group)
            with_waste = area_mm2 * (1 + waste / 100)
            roll_w = float(material.get("stock_width") or 1400)
            metres = round(with_waste / (roll_w * 1000), 2) if roll_w else 0
            lines.append(Line(
                category=category, material_code=code,
                description=f"{material['name']} - {metres}m off a "
                            f"{roll_w:.0f}mm roll",
                part_ids=ids, quantity=metres, unit="m",
                detail={"surface_m2": round(area_mm2 / 1e6, 3),
                        "roll_width_mm": roll_w,
                        "includes_waste_pct": waste},
                waste_pct=waste,
            ))

        # ---- counted items -------------------------------------------------
        elif kind == "unit":
            count = sum(max(1, int(p.get("count", 1))) for p in group)
            lines.append(Line(
                category="hardware", material_code=code,
                description=f"{material['name']}",
                part_ids=ids, quantity=count, unit="each",
                detail={}, waste_pct=0,
            ))

        # ---- sold by volume ------------------------------------------------
        else:
            volume_m3 = sum(volume_mm3(p) * max(1, int(p.get("count", 1)))
                            for p in group) / 1e9
            with_waste = round(volume_m3 * (1 + waste / 100), 4)
            lines.append(Line(
                category=category, material_code=code,
                description=f"{material['name']} - {with_waste} m³",
                part_ids=ids, quantity=with_waste, unit="m3",
                detail={"volume_m3": round(volume_m3, 4)},
                waste_pct=waste,
            ))

        # ---- weight ---------------------------------------------------------
        density = material.get("density")
        if density:
            volume_m3 = sum(volume_mm3(p) * max(1, int(p.get("count", 1)))
                            for p in group) / 1e9
            weight_kg += volume_m3 * float(density)

    # --- fasteners ----------------------------------------------------------
    tally, fastener_notes = fasteners.schedule(spec)
    notes.extend(fastener_notes)

    for code, entry in sorted(tally.items()):
        material = cat.get(code)
        unit = "ml" if code == "wood_glue_ml" else "each"
        lines.append(Line(
            category="fastener", material_code=code,
            description=entry["description"],
            part_ids=[], quantity=entry["qty"], unit=unit,
            detail={"from": entry["because"]},
            waste_pct=0,
        ))


    # Biggest-first within a category reads like a shopping list.
    lines.sort(key=lambda line: (ORDER.get(line.category, 9), -line.quantity))

    return Bill(
        lines=lines,
        notes=notes,
        total_weight_kg=round(weight_kg, 2) if weight_kg else None,
    )
