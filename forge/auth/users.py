"""Signup, login, password hashing.

Argon2id, never a fast hash. Password verification is deliberately slow; that is
the feature, not a cost to optimise away.
"""

from __future__ import annotations

import re
import unicodedata

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from .. import db

_hasher = PasswordHasher()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 6


class AuthError(Exception):
    """Raised for anything the user can see and fix."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
        return True
    except (VerifyMismatchError, VerificationError):
        return False


def normalise_email(email: str) -> str:
    return email.strip().lower()


def validate_password(password: str) -> None:
    """The one place the password rule lives.

    Signing up, resetting a forgotten password and changing a known one all
    call this. Three copies of "at least N characters" is how a reset flow ends
    up accepting a password that signup would have rejected.
    """
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )


def validate_signup(email: str, password: str, org_name: str) -> None:
    if not EMAIL_RE.match(email):
        raise AuthError("That does not look like a valid email address.")
    validate_password(password)
    if not org_name.strip():
        raise AuthError("Please give your organisation a name.")


def set_password(user_id: str, current: str, new: str) -> None:
    """Change a password the user already knows.

    The current one is required even though the session already proves who they
    are: a session can be a borrowed laptop, and changing the password is how
    an attacker with one would lock the owner out.
    """
    user = get_user(user_id)
    if user is None:
        raise AuthError("That account no longer exists.")
    if not verify_password(user["password_hash"], current or ""):
        raise AuthError("That is not your current password.")
    validate_password(new)
    if verify_password(user["password_hash"], new):
        raise AuthError("That is the password you already have.")

    db.execute("UPDATE users SET password_hash = %s WHERE id = %s",
               (hash_password(new), user_id))


def set_name(user_id: str, name: str) -> str:
    """Change the display name. Returns what was stored."""
    cleaned = " ".join((name or "").split())[:120]
    if not cleaned:
        raise AuthError("Please give a name to be known by.")
    db.execute("UPDATE users SET name = %s WHERE id = %s", (cleaned, user_id))
    return cleaned


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "org"


def _unique_slug(cur, base: str) -> str:
    slug, n = base, 1
    while True:
        cur.execute("SELECT 1 FROM orgs WHERE slug = %s", (slug,))
        if cur.fetchone() is None:
            return slug
        n += 1
        slug = f"{base}-{n}"


def get_user_by_email(email: str) -> dict | None:
    return db.fetch_one(
        "SELECT * FROM users WHERE lower(email) = lower(%s)", (normalise_email(email),)
    )


def get_user(user_id: str) -> dict | None:
    return db.fetch_one("SELECT * FROM users WHERE id = %s", (user_id,))


def create_account(email: str, password: str, name: str, org_name: str) -> dict:
    """Create a user, their org, and the admin membership joining the two.

    All three in one transaction: a user with no org, or an org with no admin,
    is a broken state that would need manual repair.

    The role is `admin`, and it matters that it is spelled the way the
    constraint expects. This inserted `owner` until migration 004 collapsed
    four roles into three and added a check constraint allowing only admin,
    manager and user. Existing rows were mapped owner -> admin by that
    migration, so every account created before it kept working and no
    developer database showed the fault - while every *new* signup violated
    the constraint. It only surfaces on a database where somebody signs up
    after 004, which is to say on a first real deployment.
    """
    email = normalise_email(email)
    validate_signup(email, password, org_name)

    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE lower(email) = %s", (email,))
        if cur.fetchone() is not None:
            raise AuthError("An account with that email already exists.")

        cur.execute(
            """
            INSERT INTO users (email, password_hash, name)
            VALUES (%s, %s, %s)
            RETURNING *
            """,
            (email, hash_password(password), name.strip()),
        )
        user = cur.fetchone()

        slug = _unique_slug(cur, slugify(org_name))
        cur.execute(
            "INSERT INTO orgs (name, slug) VALUES (%s, %s) RETURNING *",
            (org_name.strip(), slug),
        )
        org = cur.fetchone()

        cur.execute(
            """
            INSERT INTO memberships (org_id, user_id, role)
            VALUES (%s, %s, 'admin')
            """,
            (org["id"], user["id"]),
        )
        conn.commit()

    user["org"] = org
    return user


def authenticate(email: str, password: str) -> dict:
    """Verify credentials. The failure message is deliberately identical either
    way, so it cannot be used to discover which emails have accounts."""
    generic = "Email or password is incorrect."
    user = get_user_by_email(email)
    if user is None:
        # Hash anyway so a missing account and a wrong password take the same
        # time; otherwise response timing leaks which emails are registered.
        _hasher.hash(password)
        raise AuthError(generic)
    if not verify_password(user["password_hash"], password):
        raise AuthError(generic)
    return user


def orgs_for_user(user_id: str) -> list[dict]:
    return db.fetch_all(
        """
        SELECT o.*, m.role
        FROM orgs o
        JOIN memberships m ON m.org_id = o.id
        WHERE m.user_id = %s AND o.archived_at IS NULL
        ORDER BY o.created_at
        """,
        (user_id,),
    )


def touch_last_login(user_id: str) -> None:
    db.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user_id,))
