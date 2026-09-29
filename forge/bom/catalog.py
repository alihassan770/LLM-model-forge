"""The material catalogue.

Each organisation gets its own copy, seeded from the defaults below, because
prices, stock sizes and even which timbers are stocked differ by supplier and
country. A single global price list would be wrong for everyone.

Prices here are plausible placeholders, not quotes. They exist so a bill has a
shape and a total; an organisation is expected to replace them with its own.
"""

from __future__ import annotations

from .. import db

# kind:
#   linear  sold by length in stock sizes  -> cutting list
#   sheet   sold in sheets                 -> area, then sheets
#   volume  sold by the cubic metre        -> volume
#   area    sold by the running metre      -> area plus wastage
#   unit    counted items                  -> a count
DEFAULTS = [
    # code, name, kind, unit, density, stock_len, stock_w, stock_h, waste, price
    ("solid_pine", "Pine, kiln-dried", "linear", "length", 520, 2400, 70, 45, 12, 6.40),
    ("hardwood_oak", "Oak, European", "linear", "length", 720, 2400, 70, 45, 12, 21.00),
    ("hardwood_walnut", "Walnut, American", "linear", "length", 660, 2400, 70, 45, 14, 38.00),
    ("hardwood_beech", "Beech", "linear", "length", 710, 2400, 70, 45, 12, 14.50),
    ("mdf", "MDF, 18mm", "sheet", "sheet", 750, None, 2440, 18, 15, 32.00),
    ("mdf_laminate", "MDF, laminated 18mm", "sheet", "sheet", 760, None, 2440, 18, 15, 46.00),
    ("plywood", "Birch plywood, 18mm", "sheet", "sheet", 680, None, 2440, 18, 12, 58.00),
    ("mdf_walnut_veneer", "MDF, walnut veneer 18mm", "sheet", "sheet", 760, None, 2440, 18, 15, 72.00),
    ("steel_tube", "Steel tube, square section", "linear", "length", 7850, 3000, 40, 40, 8, 18.00),
    ("steel_powder_coated", "Steel, powder coated", "linear", "length", 7850, 3000, 40, 40, 8, 24.00),
    ("aluminium", "Aluminium extrusion", "linear", "length", 2700, 3000, 40, 40, 8, 29.00),
    ("fabric_linen", "Linen upholstery fabric", "area", "m", 380, None, 1400, None, 18, 27.00),
    ("fabric_cotton", "Cotton upholstery fabric", "area", "m", 350, None, 1400, None, 18, 19.00),
    ("leather", "Upholstery leather", "area", "m2", 860, None, None, None, 25, 78.00),
    ("foam_medium", "Foam, 35kg medium density", "volume", "m3", 35, None, None, None, 10, 190.00),
    ("foam_firm", "Foam, 50kg firm density", "volume", "m3", 50, None, None, None, 10, 245.00),
    ("glass_toughened", "Toughened glass, 10mm", "sheet", "sheet", 2500, None, 2000, 10, 5, 165.00),
    ("abs_plastic", "ABS plastic", "volume", "m3", 1040, None, None, None, 8, 2200.00),
    ("nylon_castor", "Nylon castor, 50mm", "unit", "each", None, None, None, None, 0, 3.20),
    ("nylon_glide", "Nylon glide / foot pad", "unit", "each", None, None, None, None, 0, 0.45),
    ("gas_strut", "Gas lift strut", "unit", "each", None, None, None, None, 0, 18.00),
    ("bracket_corner", "Corner bracket, steel", "unit", "each", None, None, None, None, 0, 1.10),
    ("unspecified", "Unspecified material", "volume", "m3", 600, None, None, None, 10, 0.0),
]

# Fasteners are ordered by length so a size can be chosen by material
# thickness rather than picked from the air.
FASTENER_DEFAULTS = [
    ("screw_25", "Wood screw, 25mm (1 inch)", 0.06),
    ("screw_30", "Wood screw, 30mm", 0.07),
    ("screw_40", "Wood screw, 40mm (1.5 inch)", 0.08),
    ("screw_50", "Wood screw, 50mm (2 inch)", 0.10),
    ("screw_65", "Wood screw, 65mm (2.5 inch)", 0.13),
    ("screw_80", "Wood screw, 80mm (3 inch)", 0.17),
    ("nail_25", "Panel pin, 25mm (1 inch)", 0.02),
    ("nail_50", "Wire nail, 50mm (2 inch)", 0.03),
    ("nail_75", "Wire nail, 75mm (3 inch)", 0.04),
    ("dowel_8x40", "Beech dowel, 8 x 40mm", 0.06),
    ("bolt_m8x60", "Bolt M8 x 60mm with nut and washer", 0.55),
    ("staple_10", "Upholstery staple, 10mm", 0.004),
    ("pocket_screw_32", "Pocket-hole screw, 32mm", 0.09),
    ("wood_glue_ml", "PVA wood glue", 0.012),   # per millilitre
]


def seed(org_id: str) -> int:
    """Install the default catalogue for an organisation. Idempotent."""
    rows = 0
    for (code, name, kind, unit, density, stock_len, stock_w, stock_h,
         waste, price) in DEFAULTS + [
            (c, n, "unit", "each", None, None, None, None, 0, p)
            for c, n, p in FASTENER_DEFAULTS]:
        db.execute(
            """
            INSERT INTO materials (org_id, code, name, kind, unit, density,
                                   stock_length, stock_width, stock_height,
                                   waste_pct, unit_price)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (org_id, code) DO NOTHING
            """,
            (org_id, code, name, kind, unit, density, stock_len, stock_w,
             stock_h, waste, price),
        )
        rows += 1
    return rows


def for_org(org_id: str) -> dict[str, dict]:
    """The catalogue as {code: row}, seeding it on first use."""
    rows = db.fetch_all(
        "SELECT * FROM materials WHERE org_id = %s AND active", (org_id,)
    )
    if not rows:
        seed(org_id)
        rows = db.fetch_all(
            "SELECT * FROM materials WHERE org_id = %s AND active", (org_id,)
        )
    return {r["code"]: r for r in rows}


def resolve(catalogue: dict[str, dict], code: str) -> dict:
    """Find the best catalogue entry for a spec's material code.

    Specs contain free-text codes the model chose - `solid_pine`,
    `mdf_walnut_veneer`, `steel_powder_coated`. An exact match is preferred,
    then a substring match either way, so `pine_softwood` still finds pine
    rather than falling to 'unspecified' and costing nothing.
    """
    code = (code or "unspecified").strip().lower()
    if code in catalogue:
        return catalogue[code]

    for key, row in catalogue.items():
        if key in code or code in key:
            return row

    # Last resort: match on the material family word.
    for word in code.replace("-", "_").split("_"):
        if len(word) < 3:
            continue
        for key, row in catalogue.items():
            if word in key:
                return row

    return catalogue.get("unspecified", {
        "code": "unspecified", "name": "Unspecified material",
        "kind": "volume", "unit": "m3", "density": 600,
        "waste_pct": 10, "unit_price": 0, "stock_length": None,
        "stock_width": None, "stock_height": None,
    })
