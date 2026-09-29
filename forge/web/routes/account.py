"""Your own account: the name you are known by, and your password.

Separate from Settings on purpose. Settings is the workspace - the AI model,
the API key, things that apply to everyone and that only an admin or manager
may touch. This page is yours, and every role has it, because your own name and
your own password are not someone else's to administer. The workspace's own
name is on Settings, next to the other things that apply to everyone.

The email address is deliberately not editable here. It is the identity every
invite, audit entry and reset link is keyed to, so changing it is a different
job - it needs the new address proven before the old one stops working, and
that is a verification flow rather than a text field.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from ... import audit
from ...auth import users
from ..deps import client_ip, current_org, require_user, verify_csrf

router = APIRouter()


def _page(request: Request, sess: dict, org: dict, **extra) -> HTMLResponse:
    from ..app import templates

    user = users.get_user(sess["user_id"]) or {}
    return templates.TemplateResponse(
        request=request, name="account.html",
        context={
            "session": sess,
            "org": org,
            "orgs": users.orgs_for_user(sess["user_id"]),
            "user": user,
            "active": "account",
            **extra,
        },
    )


@router.get("/account", response_class=HTMLResponse)
async def account(
    request: Request,
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    return _page(request, sess, org)


@router.post("/account/name")
async def change_name(
    request: Request,
    name: str = Form(""),
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    verify_csrf(request, csrf_token)
    try:
        stored = users.set_name(sess["user_id"], name)
    except users.AuthError as exc:
        return _page(request, sess, org, name_error=str(exc), name_value=name)

    audit.record(
        "user.renamed", org_id=org["id"], actor_user_id=sess["user_id"],
        target_type="user", target_id=sess["user_id"],
        metadata={"name": stored}, ip=client_ip(request),
    )
    # The rail shows the name, so it has to be re-read for this response; the
    # session dict is a snapshot taken at request start.
    sess = {**sess, "name": stored}
    return _page(request, sess, org, name_saved=stored)


@router.post("/account/password")
async def change_password(
    request: Request,
    current: str = Form(""),
    password: str = Form(""),
    confirm: str = Form(""),
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    verify_csrf(request, csrf_token)

    if password != confirm:
        return _page(request, sess, org,
                     password_error="Those two passwords differ.")
    try:
        users.set_password(sess["user_id"], current, password)
    except users.AuthError as exc:
        return _page(request, sess, org, password_error=str(exc))

    audit.record(
        "user.password_changed", org_id=org["id"],
        actor_user_id=sess["user_id"], target_type="user",
        target_id=sess["user_id"], metadata={}, ip=client_ip(request),
    )
    # Other sessions are *not* revoked here, unlike a reset: this user proved
    # they know the current password, so there is no reason to assume the
    # account is compromised and log them out of their other devices.
    return _page(request, sess, org, password_saved=True)
