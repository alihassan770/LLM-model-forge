"""Append-only activity log.

Everything an admin needs to answer 'who did what, and when' comes from this
table. Writing to it must never break the request that triggered it, so failures
are logged and swallowed.
"""

from __future__ import annotations

import json
import logging

from . import db

log = logging.getLogger(__name__)

# Events are grouped so the activity page can lead with work rather than
# bookkeeping. An admin opening this page wants to know what was *produced* - # which photo went through, which model was built - not that somebody signed in
# at nine o'clock.
#
#   work - the pipeline doing its job. This is the signal.
#   admin - deliberate configuration changes. Worth seeing, less often.
#   auth - sign-ins and sign-outs. Security-relevant, mostly background.
#   system - reads and views. Recorded for accountability, hidden by default,
#            because logging every visit to the log makes the log about itself.
CATEGORIES = {
    "work": (
        "project.", "asset.", "upload.", "extract.", "spec.",
        "question.", "job.", "build.", "export.", "bom.", "download.",
    ),
    "admin": ("settings.", "member.", "role.", "invite.", "org."),
    "auth": ("auth.", "account."),
    "system": ("admin.",),
}

# What the activity page shows unless asked otherwise.
DEFAULT_CATEGORIES = ("work", "admin")


def category_of(action: str) -> str:
    for name, prefixes in CATEGORIES.items():
        if action.startswith(prefixes):
            return name
    return "work"


def sql_prefixes(categories) -> list[str]:
    """LIKE patterns for the given categories, for use in a WHERE clause."""
    out: list[str] = []
    for name in categories:
        out.extend(f"{p}%" for p in CATEGORIES.get(name, ()))
    return out


def record(
    action: str,
    *,
    org_id: str | None = None,
    actor_user_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict | None = None,
    ip: str | None = None,
) -> None:
    try:
        db.execute(
            """
            INSERT INTO audit_log
                (org_id, actor_user_id, action, target_type, target_id, metadata, ip)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                org_id,
                actor_user_id,
                action,
                target_type,
                str(target_id) if target_id is not None else None,
                json.dumps(metadata or {}),
                ip,
            ),
        )
    except Exception:
        # An audit failure must not take down the action being audited.
        log.exception("audit write failed for action=%s", action)


def recent(
    org_id: str | None = None,
    limit: int = 50,
    categories=DEFAULT_CATEGORIES,
) -> list[dict]:
    where, params = [], []

    if org_id:
        where.append("a.org_id = %s")
        params.append(org_id)

    prefixes = sql_prefixes(categories)
    if prefixes:
        where.append("a.action LIKE ANY(%s)")
        params.append(prefixes)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return db.fetch_all(
        f"""
        SELECT a.*, u.email AS actor_email
        FROM audit_log a
        LEFT JOIN users u ON u.id = a.actor_user_id
        {clause}
        ORDER BY a.created_at DESC
        LIMIT %s
        """,
        (*params, limit),
    )
