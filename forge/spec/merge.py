"""Fold a user's answer into the specification.

Every answer produces a **new revision** rather than editing the last one. The
spec is what both the geometry and the bill of materials are derived from, so
its history has to be inspectable: what was read, what was corrected, by whom,
and when. Editing in place would make a wrong model impossible to explain.
"""

from __future__ import annotations

import copy
import json
import uuid

from .. import db
from . import schema as spec_schema


def _find_part(spec: dict, part_id: str) -> dict | None:
    return next((p for p in spec.get("parts", []) if p["id"] == part_id), None)


def apply_answer(
    spec: dict,
    part_id: str,
    dimension: str,
    value: float,
    unit: str,
) -> dict:
    """Return a copy of `spec` with the answer applied.

    The answered dimension is written with `source: "user"` and full
    confidence, and its question is removed from `missing_dimensions`.
    """
    out = copy.deepcopy(spec)

    measurement = {
        "name": dimension,
        "value": float(value),
        "unit": unit,
        "source": "user",
        "confidence": 1.0,
        "note": "Answered by the user.",
    }

    target = _find_part(out, part_id) if part_id else None
    bucket = target["dimensions"] if target is not None else out.setdefault("overall", [])

    # Replace a same-named measurement rather than appending a second one:
    # a part with two conflicting 'thickness' entries is unbuildable, and the
    # user's answer is the authoritative one.
    for i, existing in enumerate(bucket):
        if existing.get("name", "").strip().lower() == dimension.strip().lower():
            bucket[i] = measurement
            break
    else:
        bucket.append(measurement)

    out["missing_dimensions"] = [
        q for q in out.get("missing_dimensions", [])
        if not (q.get("part_id", "") == part_id
                and q.get("dimension", "").strip().lower() == dimension.strip().lower())
    ]
    return out


def save_revision(
    org_id: str,
    item_id: str,
    spec: dict,
    user_id: str,
    parent_revision: int,
    provider: str = "",
    model: str = "",
) -> dict:
    # The image list is carried forward from the revision being amended, not
    # recomputed: a question's region.image_index points into it, so a new
    # revision that dropped it would leave every remaining crop resolving
    # against nothing. Answering a question changes the numbers, never which
    # pictures they were read from.
    previous = db.fetch_one(
        """
        SELECT coalesce(max(revision), 0) + 1 AS n,
               (SELECT sources FROM specs WHERE item_id = %s
                 ORDER BY revision DESC LIMIT 1) AS sources
        FROM specs WHERE item_id = %s
        """,
        (item_id, item_id),
    )
    revision = previous["n"]
    sources = previous["sources"] or []

    return db.fetch_one(
        """
        INSERT INTO specs (id, org_id, item_id, revision, body, sources,
                           parent_revision, created_by, provider, model)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING *
        """,
        (str(uuid.uuid4()), org_id, item_id, revision, json.dumps(spec),
         json.dumps(sources), parent_revision, user_id, provider, model),
    )


def apply_default(spec: dict, part_id: str, dimension: str) -> dict:
    """Skip an optional question, recording the fact as an assumption.

    A skipped dimension has to leave a trace. Silently dropping it would mean
    the builder later invents a number with nothing on screen to say so.
    """
    out = copy.deepcopy(spec)
    question = next(
        (q for q in out.get("missing_dimensions", [])
         if q.get("part_id") == part_id
         and q.get("dimension", "").lower() == dimension.lower()),
        None,
    )
    if question is not None:
        low = question.get("suggested_min") or 0
        high = question.get("suggested_max") or 0
        if low and high:
            midpoint = round((float(low) + float(high)) / 2, 2)
            out = apply_answer(out, part_id, dimension, midpoint,
                               question.get("unit", "mm"))
            # apply_answer marks it as the user's; it is not - correct that.
            bucket = (_find_part(out, part_id) or {}).get("dimensions") \
                if part_id else out.get("overall", [])
            for m in bucket or []:
                if m["name"].lower() == dimension.lower():
                    m["source"] = "estimated"
                    m["confidence"] = 0.3
                    m["note"] = "Skipped by the user; midpoint of the expected range."
            out.setdefault("assumptions", []).append(
                f"{dimension} of {part_id or 'the object'} was not supplied; "
                f"assumed {midpoint}{question.get('unit', 'mm')}."
            )
        else:
            out["missing_dimensions"] = [
                q for q in out["missing_dimensions"] if q is not question
            ]
            out.setdefault("assumptions", []).append(
                f"{dimension} of {part_id or 'the object'} was skipped."
            )
    return out
