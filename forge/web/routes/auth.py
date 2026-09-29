"""Signup, login, logout, organisation switching, forgotten passwords.

There is no invitation flow. It was removed at the owner's request: the
link it produced sat in a readonly input rather than an anchor, so it could
not be clicked, and it was built from FORGE_BASE_URL which was unresolved
on the deployment that generated it - so the one thing it had to get right
was the thing it got wrong. Anyone who needs an account signs up, which
gives them their own workspace.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import audit, mail, orgs
from ...auth import reset, sessions, users
from ..deps import client_ip, current_user, require_user, verify_csrf

router = APIRouter()


def _templates():
    from ..app import templates

    return templates


def _render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return _templates().TemplateResponse(
        request=request, name=name, context=ctx, status_code=status_code
    )


def _safe_next(nxt: str | None) -> str:
    """Only ever redirect to a path on this site.

    An open redirect is a phishing primitive: //evil.com and https://evil.com
    are both valid values for a browser to follow.
    """
    if not nxt or not nxt.startswith("/") or nxt.startswith("//"):
        return "/"
    return nxt


# --------------------------------------------------------------------------- #
# Signup
# --------------------------------------------------------------------------- #

@router.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return _render(request, "signup.html")


@router.post("/signup")
async def signup(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    org_name: str = Form(...),
    password: str = Form(...),
):
    try:
        user = users.create_account(email, password, name, org_name)
    except users.AuthError as exc:
        return _render(
            request,
            "signup.html",
            status_code=400,
            error=str(exc),
            email=email,
            name=name,
            org_name=org_name,
        )

    token, _ = sessions.create(
        user["id"],
        user["org"]["id"],
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    users.touch_last_login(user["id"])
    audit.record(
        "account.created",
        org_id=user["org"]["id"],
        actor_user_id=user["id"],
        metadata={"email": user["email"], "org": user["org"]["name"]},
        ip=client_ip(request),
    )

    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(sessions.COOKIE_NAME, token, **sessions.cookie_kwargs())
    return response


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #

@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str | None = None):
    if current_user(request):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return _render(request, "login.html", next=next or "")


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
):
    ip = client_ip(request)
    try:
        user = users.authenticate(email, password)
    except users.AuthError as exc:
        audit.record("auth.login_failed", metadata={"email": email}, ip=ip)
        return _render(
            request, "login.html", status_code=401,
            error=str(exc), email=email, next=next,
        )

    orgs = users.orgs_for_user(user["id"])
    org_id = orgs[0]["id"] if orgs else None

    token, _ = sessions.create(
        user["id"], org_id, ip=ip, user_agent=request.headers.get("user-agent")
    )
    users.touch_last_login(user["id"])
    audit.record("auth.login", org_id=org_id, actor_user_id=user["id"], ip=ip)

    response = RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(sessions.COOKIE_NAME, token, **sessions.cookie_kwargs())
    return response


# --------------------------------------------------------------------------- #
# Logout
# --------------------------------------------------------------------------- #

@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...)):
    verify_csrf(request, csrf_token)
    token = request.cookies.get(sessions.COOKIE_NAME)
    sess = sessions.load(token)
    if sess:
        audit.record(
            "auth.logout",
            org_id=sess.get("org_id"),
            actor_user_id=sess["user_id"],
            ip=client_ip(request),
        )
    sessions.destroy(token)
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(sessions.COOKIE_NAME, path="/")
    return response


# --------------------------------------------------------------------------- #
# Joining by invitation
# --------------------------------------------------------------------------- #

@router.post("/switch-org")
async def switch_org(
    request: Request,
    org_id: str = Form(...),
    csrf_token: str = Form(...),
    sess: dict = None,
):
    sess = require_user(request)
    verify_csrf(request, csrf_token)

    # Membership is re-checked here rather than trusted from the form: the org_id
    # arrives from the browser and must never be taken at face value.
    allowed = {str(o["id"]) for o in users.orgs_for_user(sess["user_id"])}
    if org_id not in allowed:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    sessions.set_org(request.cookies[sessions.COOKIE_NAME], org_id)
    audit.record(
        "org.switched", org_id=org_id, actor_user_id=sess["user_id"],
        ip=client_ip(request),
    )
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- #
# Forgotten password
# --------------------------------------------------------------------------- #

@router.get("/forgot", response_class=HTMLResponse)
async def forgot_form(request: Request):
    if current_user(request):
        return RedirectResponse("/account", status_code=status.HTTP_303_SEE_OTHER)
    return _render(request, "forgot.html")


@router.post("/forgot")
async def forgot(
    request: Request,
    email: str = Form(""),
    csrf_token: str = Form(""),
):
    """Issue a reset link, without revealing whether the address exists.

    The response is identical for a known address, an unknown one, and a
    malformed one. Anything else makes this form a way to ask "does this person
    have an account here", which is worth more to an attacker than it sounds.
    """
    token, user = reset.request_for_email(email, ip=client_ip(request))

    if user is not None:
        link = str(request.base_url).rstrip("/") + f"/reset/{token}"
        subject, body = mail.reset_email(user.get("name") or "", link,
                                         reset.TTL_HOURS)
        outcome = mail.send(user["email"], subject, body)
        audit.record(
            "password.reset_requested",
            org_id=None, actor_user_id=str(user["id"]),
            target_type="user", target_id=str(user["id"]),
            # Never the token. Whether it was delivered is the useful fact.
            metadata={"emailed": outcome.sent,
                      "detail": outcome.detail[:120] or None},
            ip=client_ip(request),
        )

    return _render(request, "forgot.html", sent=True,
                   email_configured=mail.configured())


@router.get("/reset/{token}", response_class=HTMLResponse)
async def reset_form(request: Request, token: str):
    row = reset.lookup(token)
    if row is None:
        return _render(request, "reset.html", status_code=400, dead=True,
                       spent=reset.was_used(token))
    return _render(request, "reset.html", token=token, email=row["email"])


@router.post("/reset/{token}")
async def do_reset(
    request: Request,
    token: str,
    password: str = Form(""),
    confirm: str = Form(""),
):
    row = reset.lookup(token)
    if row is None:
        return _render(request, "reset.html", status_code=400, dead=True,
                       spent=reset.was_used(token))

    if password != confirm:
        return _render(request, "reset.html", status_code=400, token=token,
                       email=row["email"], error="Those two passwords differ.")

    try:
        done = reset.consume(token, password)
    except users.AuthError as exc:
        return _render(request, "reset.html", status_code=400, token=token,
                       email=row["email"], error=str(exc))

    if done is None:
        return _render(request, "reset.html", status_code=400, dead=True,
                       spent=True)

    audit.record(
        "password.reset_completed",
        org_id=None, actor_user_id=str(done["user_id"]),
        target_type="user", target_id=str(done["user_id"]),
        metadata={"sessions_revoked": True}, ip=client_ip(request),
    )
    # Not signed in automatically: proving they can log in with the new
    # password is the point, and it confirms the reset actually took.
    return _render(request, "login.html",
                   notice="Password changed. Sign in with your new password.")
