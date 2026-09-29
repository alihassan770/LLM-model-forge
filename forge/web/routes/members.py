"""Members and roles."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from ... import audit, orgs
from ...auth import users
from ...config import get_settings
from ..deps import client_ip, current_org, require_role, require_user, verify_csrf

router = APIRouter(prefix="/members")


def _page(request: Request, sess: dict, org: dict, **extra) -> HTMLResponse:
    from ..app import templates

    return templates.TemplateResponse(
        request=request,
        name="members.html",
        context={
            "session": sess,
            "org": org,
            "orgs": users.orgs_for_user(sess["user_id"]),
            "members": orgs.members(org["id"]),
                "assignable": orgs.ASSIGNABLE,
            "all_roles": orgs.ROLES,
            "role_help": orgs.ROLE_HELP,
            "active": "members",
            **extra,
        },
    )


@router.get("", response_class=HTMLResponse)
async def members_page(
    request: Request,
    sess: dict = Depends(require_user),
    org: dict = Depends(require_role("admin")),
):
    return _page(request, sess, org)


@router.post("/role")
async def change_role(
    request: Request,
    user_id: str = Form(...),
    role: str = Form(...),
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(require_role("admin")),
):
    verify_csrf(request, csrf_token)
    try:
        was = orgs.set_role(org["id"], sess["user_id"], org["role"], user_id, role)
    except orgs.MembershipError as exc:
        return _page(request, sess, org, error=str(exc))

    audit.record(
        "role.changed",
        org_id=org["id"], actor_user_id=sess["user_id"],
        target_type="user", target_id=user_id,
        metadata={"from": was, "to": role},
        ip=client_ip(request),
    )
    return _page(request, sess, org, notice=f"Role updated to {role}.")


@router.post("/remove")
async def remove(
    request: Request,
    user_id: str = Form(...),
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(require_role("admin")),
):
    verify_csrf(request, csrf_token)
    try:
        target = orgs.remove_member(org["id"], sess["user_id"], org["role"], user_id)
    except orgs.MembershipError as exc:
        return _page(request, sess, org, error=str(exc))

    audit.record(
        "member.removed",
        org_id=org["id"], actor_user_id=sess["user_id"],
        target_type="user", target_id=user_id,
        metadata={"email": target["email"], "role": target["role"]},
        ip=client_ip(request),
    )
    return _page(request, sess, org, notice=f"{target['email']} was removed.")
