"""Forgotten passwords: issuing a one-time link, and spending it.

The token is a 32-byte random string. Only its SHA-256 is stored, so the table
is useless to anyone who reads it - the same rule as sessions and invites. It
lives in one place: the link that goes to the person who asked.

Two properties are worth stating because they are easy to lose later:

**A request never says whether the address exists.** `request_for_email`
returns the same thing for a known and an unknown address. Anything else turns
the form into a way of testing whether someone has an account here.

**Spending a link signs out every session for that user.** If the password was
forgotten because somebody else has it, leaving their sessions alive defeats
the point of the reset.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from .. import db
from . import sessions, users

# Long enough to fetch an email and act on it; short enough that a link left
# sitting in an inbox is not a standing key to the account.
TTL_HOURS = 2

# Live links per user. A dozen requests in a row is either an impatient person
# or somebody filling the table; either way the oldest ones stop working.
MAX_LIVE = 3


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_for_user(user_id: str, requested_by: str | None = None,
                    ip: str = "") -> str:
    """Issue a reset token. Returns the token - the only time it is readable."""
    token = secrets.token_urlsafe(32)

    # Retire older live links for this user before issuing another, so a
    # forwarded or shoulder-read earlier link cannot still be spent.
    db.execute(
        """
        UPDATE password_resets SET used_at = now()
        WHERE user_id = %s AND used_at IS NULL AND expires_at > now()
          AND id NOT IN (
            SELECT id FROM password_resets
            WHERE user_id = %s AND used_at IS NULL AND expires_at > now()
            ORDER BY created_at DESC LIMIT %s
          )
        """,
        (user_id, user_id, MAX_LIVE - 1),
    )

    db.execute(
        """
        INSERT INTO password_resets (user_id, token_hash, requested_by, ip,
                                     expires_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (user_id, _hash(token), requested_by, ip[:100],
         _now() + timedelta(hours=TTL_HOURS)),
    )
    return token


def request_for_email(email: str, ip: str = "") -> tuple[str | None, dict | None]:
    """Issue a link for an address, if it belongs to anyone.

    Returns `(token, user)` or `(None, None)`. The caller **must** show the
    same message either way - see the module docstring.
    """
    user = users.get_user_by_email(users.normalise_email(email))
    if user is None:
        return None, None
    return create_for_user(str(user["id"]), requested_by=str(user["id"]), ip=ip), user


def lookup(token: str) -> dict | None:
    """The live reset this token names, with its user, or None."""
    if not token:
        return None
    return db.fetch_one(
        """
        SELECT r.*, u.email, u.name
        FROM password_resets r
        JOIN users u ON u.id = r.user_id
        WHERE r.token_hash = %s AND r.used_at IS NULL AND r.expires_at > now()
        """,
        (_hash(token),),
    )


def was_used(token: str) -> bool:
    """True when the token existed but has already been spent or has expired.

    Lets the page say "that link has already been used" instead of "invalid
    link", which is the difference between a person understanding what
    happened and a person emailing support.
    """
    if not token:
        return False
    row = db.fetch_one(
        "SELECT 1 FROM password_resets WHERE token_hash = %s",
        (_hash(token),),
    )
    return row is not None


def consume(token: str, new_password: str) -> dict | None:
    """Set the password and spend the link. Returns the user, or None.

    Every session for that user is destroyed, including the one doing the
    resetting: the password may have been forgotten because someone else has
    it, and a live session would survive the reset.
    """
    row = lookup(token)
    if row is None:
        return None

    users.validate_password(new_password)

    with db.connection() as conn, conn.cursor() as cur:
        # Spend the token in the same transaction as the password change, and
        # only if it is still unspent - two simultaneous submissions of the
        # same link must not both succeed.
        cur.execute(
            """
            UPDATE password_resets SET used_at = now()
            WHERE id = %s AND used_at IS NULL
            RETURNING id
            """,
            (row["id"],),
        )
        if cur.fetchone() is None:
            return None
        cur.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (users.hash_password(new_password), row["user_id"]),
        )

    sessions.destroy_all_for_user(str(row["user_id"]))
    return row


def purge_expired() -> int:
    """Drop rows that are long dead. Called from the same place as sessions."""
    return db.execute(
        "DELETE FROM password_resets WHERE expires_at < now() - interval '30 days'"
    )
