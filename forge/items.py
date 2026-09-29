"""Items - one physical object being modelled.

An item carries the photographs, the specification, the builds and the bill,
because all of those are properties of one object. Its parent project only
groups items together and totals them.
"""

from __future__ import annotations

import hashlib
import uuid

from . import db
from .ingest import detect
from .storage import keys
from .storage.backend import for_org

ITEM_STATUS = STATUS_LABEL = {
    "draft": "Draft",
    "extracting": "Reading the file",
    "questions": "Needs your input",
    "ready": "Ready to build",
    "building": "Building model",
    "complete": "Complete",
    "failed": "Failed",
}

ROLE_LABEL = {
    "primary": "Main photo",
    "additional_view": "Extra view",
    "spec_sheet": "Spec sheet",
}


class ItemError(Exception):
    """Something the user can see and fix."""


def create(org_id: str, project_id: str, user_id: str, name: str,
           category: str = "", notes: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise ItemError("Give the item a name.")
    if len(name) > 120:
        raise ItemError("That name is too long (120 characters maximum).")

    position = db.fetch_one(
        "SELECT coalesce(max(position), 0) + 1 AS n FROM items WHERE project_id = %s",
        (project_id,),
    )["n"]
    return db.fetch_one(
        """
        INSERT INTO items (org_id, project_id, created_by, name, category,
                           notes, position)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (org_id, project_id, user_id, name, category.strip()[:80],
         notes.strip()[:2000], position),
    )


def for_project(org_id: str, project_id: str) -> list[dict]:
    """Every item in a project, in the order they should be shown."""
    return db.fetch_all(
        """
        SELECT i.*,
               u.email AS created_by_email,
               (SELECT count(*) FROM assets a WHERE a.item_id = i.id) AS asset_count,
               (SELECT count(*) FROM jobs j WHERE j.item_id = i.id
                  AND j.state = 'done') AS build_count,
               (SELECT count(*) FROM boms b WHERE b.item_id = i.id) AS bom_count,
               (SELECT a.storage_key FROM artifacts a
                  JOIN jobs j ON j.id = a.job_id
                 WHERE j.item_id = i.id AND a.format = 'preview_perspective'
                 ORDER BY a.created_at DESC LIMIT 1) AS thumb_key,
               (SELECT a.id FROM artifacts a
                  JOIN jobs j ON j.id = a.job_id
                 WHERE j.item_id = i.id AND a.format = 'preview_perspective'
                 ORDER BY a.created_at DESC LIMIT 1) AS thumb_id
        FROM items i
        LEFT JOIN users u ON u.id = i.created_by
        WHERE i.org_id = %s AND i.project_id = %s AND i.archived_at IS NULL
        ORDER BY i.position, i.created_at
        """,
        (org_id, project_id),
    )


def get(org_id: str, item_id: str) -> dict | None:
    """An item with its parent project, or None.

    org_id is part of the predicate rather than checked afterwards: an item
    from another tenant simply does not match, so there is no moment when the
    row exists in memory before the check runs.
    """
    return db.fetch_one(
        """
        SELECT i.*, u.email AS created_by_email,
               p.name AS project_name, p.id AS project_id_ref, p.client
        FROM items i
        JOIN projects p ON p.id = i.project_id
        LEFT JOIN users u ON u.id = i.created_by
        WHERE i.org_id = %s AND i.id = %s AND i.archived_at IS NULL
        """,
        (org_id, item_id),
    )


def assets(item_id: str) -> list[dict]:
    return db.fetch_all(
        """
        SELECT a.*, u.email AS uploaded_by_email
        FROM assets a
        LEFT JOIN users u ON u.id = a.uploaded_by
        WHERE a.item_id = %s
        ORDER BY a.created_at
        """,
        (item_id,),
    )


def archive(org_id: str, item_id: str) -> None:
    db.execute(
        "UPDATE items SET archived_at = now() WHERE org_id = %s AND id = %s",
        (org_id, item_id),
    )


def touch(item_id: str) -> None:
    db.execute("UPDATE items SET updated_at = now() WHERE id = %s", (item_id,))


# --------------------------------------------------------------------------- #
# Uploads
# --------------------------------------------------------------------------- #

def add_asset(
    org_id: str,
    item_id: str,
    user_id: str,
    filename: str,
    declared_mime: str,
    data: bytes,
    role: str = "primary",
) -> dict:
    """Validate, store, and record one uploaded file.

    Raises detect.UnsupportedFile with a message written for the uploader.
    """
    found = detect.inspect(data, declared_mime)

    digest = hashlib.sha256(data).hexdigest()
    duplicate = db.fetch_one(
        "SELECT original_name FROM assets WHERE item_id = %s AND sha256 = %s",
        (item_id, digest),
    )
    if duplicate is not None:
        raise ItemError(
            f"That exact file is already attached to this item "
            f"as “{duplicate['original_name']}”."
        )

    asset_id = str(uuid.uuid4())
    key = keys.upload(org_id, asset_id, found.extension)

    # Storage first. A row pointing at bytes that were never written is a
    # broken record that every later stage has to defend against; an orphaned
    # object is just wasted space a lifecycle rule will reclaim.
    for_org(org_id).put(key, data, found.mime)

    try:
        row = db.fetch_one(
            """
            INSERT INTO assets
                (id, org_id, item_id, uploaded_by, original_name,
                 declared_mime, detected_mime, storage_key, bytes, sha256,
                 width, height, page_count, role)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (asset_id, org_id, item_id, user_id, filename[:255],
             declared_mime[:100], found.mime, key, len(data), digest,
             found.width, found.height, found.page_count, role),
        )
    except Exception:
        for_org(org_id).delete(key)
        raise

    touch(item_id)
    return row


def delete_asset(org_id: str, asset_id: str) -> dict | None:
    row = db.fetch_one(
        "SELECT * FROM assets WHERE org_id = %s AND id = %s", (org_id, asset_id)
    )
    if row is None:
        return None
    db.execute("DELETE FROM assets WHERE id = %s", (asset_id,))
    try:
        for_org(org_id).delete(row["storage_key"])
    except Exception:
        # The record is gone, which is what the user asked for. A stranded
        # object is a cleanup problem, not a reason to fail their request.
        pass
    return row


def human_size(n: int | None) -> str:
    if not n:
        return " - "
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def latest_spec(item_id: str) -> dict | None:
    return db.fetch_one(
        """
        SELECT s.*, u.email AS created_by_email
        FROM specs s LEFT JOIN users u ON u.id = s.created_by
        WHERE s.item_id = %s
        ORDER BY s.revision DESC LIMIT 1
        """,
        (item_id,),
    )


def sync_questions(org_id: str, item_id: str, spec_id: str, spec: dict) -> int:
    """Store this spec's missing dimensions as answerable questions.

    Scoped to the spec revision rather than the project, so a re-run produces a
    fresh set instead of merging with answers given against an older reading of
    the object.
    """
    rows = spec.get("missing_dimensions", [])
    for q in rows:
        db.execute(
            """
            INSERT INTO questions
                (org_id, spec_id, item_id, part_id, dimension, question_text,
                 why_needed, unit, suggested_min, suggested_max, blocking)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (org_id, spec_id, item_id, q.get("part_id", ""), q["dimension"],
             q.get("question", ""), q.get("why_needed", ""), q.get("unit", "mm"),
             q.get("suggested_min") or None, q.get("suggested_max") or None,
             bool(q.get("blocking", True))),
        )
    return len(rows)


def open_questions(item_id: str, spec_id: str) -> list[dict]:
    return db.fetch_all(
        """
        SELECT * FROM questions
        WHERE item_id = %s AND spec_id = %s AND answered_at IS NULL
        ORDER BY blocking DESC, created_at
        """,
        (item_id, spec_id),
    )


FORMAT_USE = {
    "blend": "Open and edit every part",
    "fbx": "Import into 3ds Max or Maya",
    "obj": "Opens almost anywhere",
    "glb": "Share or view in a browser",
    "stl": "Send to a 3D printer",
    "mtl": "Keep next to the .obj",
}

FORMAT_LABEL = {
    "blend": "Blender",
    "fbx": "FBX - 3ds Max, Maya",
    "obj": "OBJ - universal",
    "glb": "glTF - web preview",
    "stl": "STL - 3D printing",
    "mtl": "MTL - OBJ materials",
}


def jobs_for(item_id: str, limit: int = 5) -> list[dict]:
    rows = db.fetch_all(
        """
        SELECT j.*, u.email AS requested_by_email
        FROM jobs j LEFT JOIN users u ON u.id = j.requested_by
        WHERE j.item_id = %s
        ORDER BY j.created_at DESC
        LIMIT %s
        """,
        (item_id, limit),
    )
    for row in rows:
        every = db.fetch_all(
            """
            SELECT * FROM artifacts WHERE job_id = %s
            ORDER BY CASE format
                       WHEN 'blend' THEN 0 WHEN 'fbx' THEN 1 WHEN 'obj' THEN 2
                       WHEN 'glb' THEN 3 WHEN 'stl' THEN 4 ELSE 5 END
            """,
            (row["id"],),
        )
        row["artifacts"] = [a for a in every
                            if not a["format"].startswith("preview_")]
        previews = {a["format"].removeprefix("preview_"): a
                    for a in every if a["format"].startswith("preview_")}
        # Perspective first - it is the one that shows what the thing is.
        row["previews"] = [previews[v] for v in
                           ("perspective", "front", "side", "top")
                           if v in previews]
    return rows
