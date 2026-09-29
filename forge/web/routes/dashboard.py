"""The signed-in landing page."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ... import audit, db, settings_store
from ...auth import users
from ..deps import current_org, require_user

router = APIRouter()


def greeting_for(hour: int) -> str:
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


def first_name(session: dict) -> str:
    """A name to greet someone by, falling back sensibly.

    An empty name field is common - it is optional at signup - so fall back to
    the part of the email before the @, title-cased. Greeting someone as
    'there' is worse than greeting them by a username they chose.
    """
    name = (session.get("name") or "").strip()
    if name:
        return name.split()[0]
    local = (session.get("email") or "").split("@")[0]
    local = local.replace(".", " ").replace("_", " ").replace("-", " ").strip()
    return local.title() if local else "there"


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    from ..app import templates

    now = datetime.now()

    # Only numbers we actually have. A dashboard that invents a metric to fill
    # a tile teaches people to distrust the ones that are real.
    stats = db.fetch_one(
        """
        SELECT
          (SELECT count(*) FROM memberships WHERE org_id = %s) AS members,
          (SELECT count(*) FROM audit_log   WHERE org_id = %s) AS events,
          (SELECT count(*) FROM projects
            WHERE org_id = %s AND archived_at IS NULL) AS projects,
          (SELECT count(*) FROM jobs
            WHERE org_id = %s AND state = 'done') AS builds
        """,
        (org["id"], org["id"], org["id"], org["id"]),
    ) or {"members": 0, "events": 0, "projects": 0, "builds": 0}

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "session": sess,
            "org": org,
            "orgs": users.orgs_for_user(sess["user_id"]),
            "activity": audit.recent(org_id=org["id"], limit=10),
            "ai": settings_store.ai_config(org["id"]),
            "greeting": greeting_for(now.hour),
            "first_name": first_name(sess),
            "today": now,
            "stats": stats,
            "active": "dashboard",
        },
    )
