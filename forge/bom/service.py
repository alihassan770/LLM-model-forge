"""Compute a materials list, store it, and ask the model what to buy.

Quantities and sizes are arithmetic, computed in `takeoff`. The model sees the
finished figures and answers two questions only: which grade and stock size to
order, and what the list cannot contain. It supplies no numbers, so the list
stays auditable and cannot quietly disagree with the geometry.

No pricing anywhere. A price list belongs to a supplier and goes stale; a
cutting list does not.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import audit, db, settings_store
from ..vision.client import complete_json
from . import takeoff

ADVICE_PROMPT = (Path(__file__).parent / "prompts" / "advice.md").read_text()

ADVICE_SCHEMA = {
    "type": "object",
    "properties": {
        "grades": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "material_code": {"type": "string"},
                    "buy": {
                        "type": "string",
                        "description": "The grade and stock size to order, in "
                                       "one line. No prices.",
                    },
                },
                "required": ["material_code", "buy"],
                "additionalProperties": False,
            },
        },
        "missing": {
            "type": "array",
            "items": {"type": "string"},
            "description": "One line each: what the object needs that the "
                           "calculated list cannot contain.",
        },
    },
    "required": ["grades", "missing"],
    "additionalProperties": False,
}


def build_bill(org_id: str, item_id: str, spec_row: dict, user_id: str,
               job_id: str | None = None, with_advice: bool = True) -> dict:
    bill = takeoff.compute(spec_row["body"], org_id)

    advice = None
    if with_advice:
        advice = _ask_for_advice(org_id, user_id, spec_row["body"], bill)

    row = db.fetch_one(
        """
        INSERT INTO boms (org_id, item_id, job_id, spec_revision,
                          total_weight, advice, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        RETURNING *
        """,
        (org_id, item_id, job_id, spec_row["revision"], bill.total_weight_kg,
         json.dumps(advice) if advice else None, user_id),
    )

    for order, line in enumerate(bill.lines):
        db.execute(
            """
            INSERT INTO bom_lines (bom_id, org_id, category, material_code,
                                   description, part_ids, quantity, unit,
                                   detail, waste_pct, sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (row["id"], org_id, line.category, line.material_code,
             line.description, line.part_ids, line.quantity, line.unit,
             json.dumps(line.detail), line.waste_pct, order),
        )

    audit.record(
        "bom.generated", org_id=org_id, actor_user_id=user_id,
        target_type="item", target_id=item_id,
        metadata={"spec_revision": spec_row["revision"],
                  "lines": len(bill.lines)},
    )

    return {"ok": True, "bom_id": str(row["id"]), "bill": bill,
            "advice": advice, "notes": bill.notes}


def _ask_for_advice(org_id: str, user_id: str, spec: dict, bill) -> dict | None:
    """Optional. A failure here must not lose a bill that is already correct."""
    config = settings_store.ai_config(org_id)
    if not config["key"]:
        return None

    summary = {
        "object": spec.get("object", {}),
        "lines": [
            {"category": line.category, "material": line.material_code,
             "description": line.description, "quantity": line.quantity,
             "unit": line.unit}
            for line in bill.lines
        ],
        "parts": [
            {"id": p["id"], "name": p.get("display_name"),
             "material": p.get("material"), "count": p.get("count", 1)}
            for p in spec.get("parts", [])
        ],
    }

    result = complete_json(
        provider=config["provider"], model=config["model"],
        api_key=config["key"], system=ADVICE_PROMPT,
        user_text="Materials list, already calculated. What to buy, and what is missing.\n\n"
                  + json.dumps(summary, indent=2),
        images=[], schema=ADVICE_SCHEMA, max_tokens=12000,
        # A materials list needs far less thinking than reading a photograph,
        # and this call is made after every build. One step down from whatever
        # extraction uses, never above it.
        effort=("low" if config.get("effort") in ("high", "max")
                else config.get("effort", "")),
    )

    db.execute(
        """
        INSERT INTO llm_calls (org_id, actor_user_id, provider, model, purpose,
                               input_tokens, output_tokens, cost_usd,
                               stop_reason, latency_ms, ok, error)
        VALUES (%s,%s,%s,%s,'bom_advice',%s,%s,%s,%s,%s,%s,%s)
        """,
        (org_id, user_id, config["provider"].id, config["model"].id,
         result.input_tokens, result.output_tokens, result.cost_usd,
         result.stop_reason, result.latency_ms, result.ok,
         result.error or None),
    )

    return result.data if result.ok else None


def latest(item_id: str) -> dict | None:
    row = db.fetch_one(
        """
        SELECT b.*, u.email AS created_by_email
        FROM boms b LEFT JOIN users u ON u.id = b.created_by
        WHERE b.item_id = %s ORDER BY b.created_at DESC LIMIT 1
        """,
        (item_id,),
    )
    if row is None:
        return None
    row["lines"] = db.fetch_all(
        "SELECT * FROM bom_lines WHERE bom_id = %s ORDER BY sort_order",
        (row["id"],),
    )
    return row


def to_csv(bom: dict) -> str:
    import csv
    import io

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Category", "Material", "Description", "Quantity", "Unit",
                     "Sizes", "For parts"])
    for line in bom["lines"]:
        detail = line["detail"] or {}
        sizes = ""
        if detail.get("pieces"):
            sizes = (f"cut {', '.join(str(p) + 'mm' for p in detail['pieces'])} "
                     f"from {detail.get('stock_length_mm', 0):.0f}mm"
                     + (f" ({detail['section']})" if detail.get("section") else ""))
        elif detail.get("sheet_size_mm"):
            sizes = f"sheets {detail['sheet_size_mm']}"
        elif detail.get("roll_width_mm"):
            sizes = f"{detail['roll_width_mm']:.0f}mm roll"
        writer.writerow([
            line["category"], line["material_code"], line["description"],
            line["quantity"], line["unit"], sizes,
            " ".join(line["part_ids"] or []),
        ])
    if bom.get("total_weight"):
        writer.writerow([])
        writer.writerow(["", "", "Estimated weight (kg)", bom["total_weight"]])
    return out.getvalue()


CATEGORY_LABEL = {
    "timber": "Timber",
    "sheet": "Sheet materials",
    "fabric": "Fabric & covering",
    "volume": "Foam & bulk",
    "hardware": "Hardware",
    "fastener": "Fasteners & adhesive",
}
