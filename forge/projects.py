"""Projects - a grouping of items.

A project is a job, a room, a client, a fit-out: "Office fit-out",
"Apartment 3B". The objects being modelled are its *items*, and they carry the
photos, specifications, models and bills. This module only groups and totals
them.

Keeping the two apart matters because almost everything downstream belongs to
one physical object. A specification revision, a cutting list, a screw count - none of those mean anything spread across a chair and a sofa at once.
"""

from __future__ import annotations

from . import db


class ProjectError(Exception):
    """Something the user can see and fix."""


def create(org_id: str, user_id: str, name: str, client: str = "",
           notes: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise ProjectError("Give the project a name.")
    if len(name) > 120:
        raise ProjectError("That name is too long (120 characters maximum).")

    return db.fetch_one(
        """
        INSERT INTO projects (org_id, created_by, name, client, notes)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *
        """,
        (org_id, user_id, name, client.strip()[:120], notes.strip()[:2000]),
    )


def listing(org_id: str, limit: int = 200) -> list[dict]:
    """Projects with their roll-up figures.

    The counts are subqueries rather than joins, so a project with no items
    still appears - a project you have just created and not filled in yet is
    exactly when you most want to see it.

    Each row also carries the names of the items inside it, because that is
    what tells you which project is which at a glance; "3 items" does not.
    """
    return db.fetch_all(
        """
        SELECT p.*,
               u.email AS created_by_email,
               (SELECT count(*) FROM items i
                 WHERE i.project_id = p.id AND i.archived_at IS NULL) AS item_count,
               (SELECT count(*) FROM items i
                 WHERE i.project_id = p.id AND i.archived_at IS NULL
                   AND i.status = 'complete') AS complete_count,
               (SELECT count(*) FROM assets a
                  JOIN items i ON i.id = a.item_id
                 WHERE i.project_id = p.id) AS asset_count,
               (SELECT count(*) FROM questions q
                  JOIN items i ON i.id = q.item_id
                 WHERE i.project_id = p.id
                   AND i.archived_at IS NULL
                   AND q.answered_at IS NULL) AS open_questions,
               (SELECT array_agg(i.name ORDER BY i.position, i.created_at)
                  FROM items i
                 WHERE i.project_id = p.id
                   AND i.archived_at IS NULL) AS item_names
        FROM projects p
        LEFT JOIN users u ON u.id = p.created_by
        WHERE p.org_id = %s AND p.archived_at IS NULL
        ORDER BY p.created_at DESC
        LIMIT %s
        """,
        (org_id, limit),
    )


def get(org_id: str, project_id: str) -> dict | None:
    return db.fetch_one(
        """
        SELECT p.*, u.email AS created_by_email
        FROM projects p
        LEFT JOIN users u ON u.id = p.created_by
        WHERE p.org_id = %s AND p.id = %s AND p.archived_at IS NULL
        """,
        (org_id, project_id),
    )


def totals(org_id: str, project_id: str) -> dict:
    """What the project adds up to across its items.

    Each item contributes its most recent bill only - recalculating a bill
    replaces the previous figure rather than adding to it.
    """
    # Deliberately NOT named "items": Jinja resolves an attribute before a
    # key, so `totals.items` in a template returns dict.items - the method - # and renders as "<built-in method items of dict object>". Any key that
    # collides with a dict method (items, keys, values, get, copy) is a trap.
    row = db.fetch_one(
        """
        SELECT
          count(*) AS item_count,
          count(*) FILTER (WHERE i.status = 'complete') AS complete,
          count(*) FILTER (WHERE i.status = 'questions') AS needs_input,
          coalesce(sum(latest.total_weight), 0) AS weight
        FROM items i
        LEFT JOIN LATERAL (
             SELECT b.total_weight FROM boms b WHERE b.item_id = i.id
              ORDER BY b.created_at DESC LIMIT 1
        ) latest ON true
        WHERE i.org_id = %s AND i.project_id = %s AND i.archived_at IS NULL
        """,
        (org_id, project_id),
    )
    return row or {"item_count": 0, "complete": 0, "needs_input": 0, "weight": 0}


def archive(org_id: str, project_id: str) -> None:
    """Archive a project and everything in it.

    Items are archived alongside it: an item whose project is gone would
    otherwise be unreachable but still counted.
    """
    db.execute(
        "UPDATE items SET archived_at = now() WHERE org_id = %s AND project_id = %s",
        (org_id, project_id),
    )
    db.execute(
        "UPDATE projects SET archived_at = now() WHERE org_id = %s AND id = %s",
        (org_id, project_id),
    )
