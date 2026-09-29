"""Server-side sessions, stored in Postgres.

The cookie carries an opaque random token; the database stores only its SHA-256.
Sessions are therefore revocable server-side, and a database dump does not hand
an attacker a set of live logins.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from .. import db
from ..config import get_settings

COOKIE_NAME = "forge_session"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(
    user_id: str,
    org_id: str | None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, str]:
    """Create a session. Returns (cookie_token, csrf_token)."""
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(
        hours=get_settings().session_hours
    )
    db.execute(
        """
        INSERT INTO sessions
            (token_hash, user_id, org_id, csrf_token, expires_at, ip, user_agent)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (_hash(token), user_id, org_id, csrf, expires, ip, (user_agent or "")[:500]),
    )
    return token, csrf


def load(token: str | None) -> dict | None:
    """Return the session joined to its user, or None if absent or expired."""
    if not token:
        return None
    row = db.fetch_one(
        """
        SELECT s.token_hash, s.user_id, s.org_id, s.csrf_token, s.expires_at,
               u.email, u.name, u.is_superadmin
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = %s AND s.expires_at > now()
        """,
        (_hash(token),),
    )
    return row


def set_org(token: str, org_id: str) -> None:
    db.execute(
        "UPDATE sessions SET org_id = %s WHERE token_hash = %s", (org_id, _hash(token))
    )


def destroy(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = %s", (_hash(token),))


def destroy_all_for_user(user_id: str) -> None:
    """Called on password change - every other device is signed out."""
    db.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))


def purge_expired() -> int:
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE expires_at <= now()")
        conn.commit()
        return cur.rowcount


def cookie_kwargs() -> dict:
    settings = get_settings()
    return {
        "httponly": True,
        # Must be True wherever TLS terminates upstream (Railway). Over plain
        # http://localhost it must be False or the browser drops the cookie and
        # login appears to silently fail.
        "secure": settings.secure_cookies,
        "samesite": "lax",
        "max_age": settings.session_hours * 3600,
        "path": "/",
    }
