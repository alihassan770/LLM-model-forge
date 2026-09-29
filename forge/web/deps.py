"""Request-scoped dependencies: who is signed in, which tenant they act as.

Every route that touches business data goes through require_user / require_org.
There is no code path that reads tenant data without an explicit org_id.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from ..auth import sessions, users

# Ordered by authority. require_role("manager") therefore admits managers and
# admins, and require_role("admin") admits only admins.
ROLE_RANK = {"user": 0, "manager": 1, "admin": 2}


class NotAuthenticated(Exception):
    """Raised instead of a 401 so the handler can redirect to the login page."""


def client_ip(request: Request) -> str | None:
    # Railway sits behind a proxy, so the socket address is the proxy's.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def current_session(request: Request) -> dict | None:
    token = request.cookies.get(sessions.COOKIE_NAME)
    return sessions.load(token)


def current_user(request: Request) -> dict | None:
    sess = current_session(request)
    if sess is None:
        return None
    request.state.session = sess
    return sess


def require_user(request: Request) -> dict:
    sess = current_user(request)
    if sess is None:
        raise NotAuthenticated()
    return sess


def current_org(request: Request, sess: dict = Depends(require_user)) -> dict:
    """Resolve the acting tenant and the user's role in it."""
    if not sess.get("org_id"):
        raise NotAuthenticated()
    org = None
    for candidate in users.orgs_for_user(sess["user_id"]):
        if str(candidate["id"]) == str(sess["org_id"]):
            org = candidate
            break
    if org is None:
        # Membership was revoked, or the org was archived, while the session
        # was still live. Treat it as signed out rather than 500.
        raise NotAuthenticated()
    request.state.org = org
    return org


def require_role(minimum: str):
    """Dependency factory enforcing a minimum role.

    Hiding a button in a template is not access control; this is.
    """

    def _check(org: dict = Depends(current_org)) -> dict:
        if ROLE_RANK.get(org["role"], -1) < ROLE_RANK[minimum]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires the {minimum} role.",
            )
        return org

    return _check


def require_superadmin(sess: dict = Depends(require_user)) -> dict:
    if not sess.get("is_superadmin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed.")
    return sess


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #

def verify_csrf(request: Request, submitted: str | None) -> None:
    sess = getattr(request.state, "session", None) or current_session(request)
    expected = sess.get("csrf_token") if sess else None
    if not expected or not submitted or not secrets.compare_digest(
        str(expected), str(submitted)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Your session expired. Please reload the page and try again.",
        )


def redirect_to_login(request: Request) -> RedirectResponse:
    nxt = request.url.path
    target = "/login" if nxt in ("/", "/login") else f"/login?next={nxt}"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
