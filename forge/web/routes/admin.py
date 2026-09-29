"""Activity dashboard.

Two audiences, one page:

  * an org admin sees everything that happened inside their own organisation;
  * a platform superadmin sees every organisation.

The scoping is decided here from the session, never from a query parameter, so
there is no URL an org admin can construct that shows them another tenant.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from ... import audit, db
from ...auth import users
from ..deps import current_org, require_role, require_user

router = APIRouter(prefix="/admin")

PAGE_SIZE = 40


@router.get("/activity", response_class=HTMLResponse)
async def activity(
    request: Request,
    action: str = Query("", max_length=60),
    actor: str = Query("", max_length=120),
    show: str = Query("", max_length=20),
    page: int = Query(1, ge=1, le=500),
    sess: dict = Depends(require_user),
    # A manager sees their organisation's activity. A platform superadmin also
    # passes this check and is widened to every organisation below.
    org: dict = Depends(require_role("manager")),
):
    from ..app import templates

    platform_wide = bool(sess.get("is_superadmin"))

    where = []
    params: list = []

    if not platform_wide:
        where.append("a.org_id = %s")
        params.append(org["id"])

    if action:
        where.append("a.action ILIKE %s")
        params.append(f"%{action}%")

    if actor:
        where.append("u.email ILIKE %s")
        params.append(f"%{actor}%")

    # Default to the work the pipeline actually did, plus deliberate config
    # changes. Sign-ins and page views are still recorded - they are the audit
    # trail - but they are not what an admin opens this page to find, and left
    # unfiltered they bury the handful of rows that matter.
    if show != "all":
        prefixes = audit.sql_prefixes(audit.DEFAULT_CATEGORIES)
        where.append("a.action LIKE ANY(%s)")
        params.append(prefixes)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    offset = (page - 1) * PAGE_SIZE

    rows = db.fetch_all(
        f"""
        SELECT a.id, a.action, a.target_type, a.target_id, a.metadata,
               a.ip, a.created_at,
               u.email AS actor_email, u.name AS actor_name,
               o.name  AS org_name
        FROM audit_log a
        LEFT JOIN users u ON u.id = a.actor_user_id
        LEFT JOIN orgs  o ON o.id = a.org_id
        {clause}
        ORDER BY a.created_at DESC
        LIMIT %s OFFSET %s
        """,
        (*params, PAGE_SIZE + 1, offset),
    )

    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    totals = db.fetch_one(
        f"""
        SELECT count(*) AS events,
               count(DISTINCT a.actor_user_id) AS actors
        FROM audit_log a
        LEFT JOIN users u ON u.id = a.actor_user_id
        {clause}
        """,
        tuple(params),
    ) or {"events": 0, "actors": 0}

    # The filter dropdown lists every action in scope, deliberately ignoring the
    # current action filter - otherwise choosing one action would leave it as the
    # only option and there would be no way back.
    if platform_wide:
        action_options = db.fetch_all(
            """
            SELECT action, count(*) AS n FROM audit_log
            GROUP BY action ORDER BY n DESC LIMIT 25
            """
        )
    else:
        action_options = db.fetch_all(
            """
            SELECT action, count(*) AS n FROM audit_log
            WHERE org_id = %s
            GROUP BY action ORDER BY n DESC LIMIT 25
            """,
            (org["id"],),
        )

    # Reading other people's activity is itself worth recording - but it is a
    # `admin.` event, so the default view does not show it. Otherwise opening
    # this page adds a row to the page you are reading, and the log slowly
    # becomes a record of itself.
    audit.record(
        "admin.activity_viewed",
        org_id=None if platform_wide else org["id"],
        actor_user_id=sess["user_id"],
        metadata={"scope": "platform" if platform_wide else "org"},
    )

    return templates.TemplateResponse(
        request=request,
        name="admin_activity.html",
        context={
            "session": sess,
            "org": org,
            "orgs": users.orgs_for_user(sess["user_id"]),
            "rows": rows,
            "totals": totals,
            "action_options": action_options,
            "platform_wide": platform_wide,
            "filter_action": action,
            "filter_actor": actor,
            "show": show,
            "page": page,
            "has_next": has_next,
            "active": "activity",
        },
    )
