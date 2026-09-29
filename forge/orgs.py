"""Membership: who is in an organisation, and what they may do.

Three roles, separating using the product from configuring it from deciding
who may do either:

  * admin - full access, including access rights
  * manager - full access to settings and activity, but not access rights
  * user - uses the app; no settings, no member management

The rules encoded here stop an organisation locking itself out:

  * the last admin cannot be demoted or removed;
  * nobody can change their own role, or remove themselves.
"""

from __future__ import annotations


from . import db

ROLES = ("admin", "manager", "user")
ASSIGNABLE = ROLES  # every role can be granted by invitation

ROLE_HELP = {
    "admin": "Full access, including who may do what.",
    "manager": "Full access to settings and activity. Cannot change access rights.",
    "user": "Uses the app. No settings, no member management.",
}

INVITE_DAYS = 14


class MembershipError(Exception):
    """Raised for anything an admin can see and correct."""


class OrgError(Exception):
    """Something about the workspace itself that the user can fix."""


def rename(org_id: str, name: str) -> str:
    """Change the workspace's display name. Returns what was stored.

    The slug is left alone deliberately. Nothing routes by it and no storage
    key contains it - keys lead with `org_id` - so it is an internal label
    only, and changing it would be churn with a small chance of breaking
    something for no gain.
    """
    cleaned = " ".join((name or "").split())[:120]
    if not cleaned:
        raise OrgError("The workspace needs a name.")

    row = db.fetch_one(
        "UPDATE orgs SET name = %s WHERE id = %s AND archived_at IS NULL "
        "RETURNING name",
        (cleaned, org_id),
    )
    if row is None:
        raise OrgError("That workspace no longer exists.")
    return row["name"]


def members(org_id: str) -> list[dict]:
    return db.fetch_all(
        """
        SELECT u.id, u.email, u.name, u.last_login_at, u.is_superadmin,
               m.role, m.joined_at
        FROM memberships m
        JOIN users u ON u.id = m.user_id
        WHERE m.org_id = %s
        ORDER BY
          CASE m.role WHEN 'admin' THEN 0 WHEN 'manager' THEN 1 ELSE 2 END,
          u.email
        """,
        (org_id,),
    )


def admin_count(org_id: str) -> int:
    row = db.fetch_one(
        "SELECT count(*) AS n FROM memberships WHERE org_id = %s AND role = 'admin'",
        (org_id,),
    )
    return int(row["n"]) if row else 0


def set_role(org_id: str, actor_id: str, actor_role: str, user_id: str, role: str):
    if role not in ROLES:
        raise MembershipError("That is not a valid role.")

    if str(actor_id) == str(user_id):
        # Otherwise the only admin could demote themselves and lock the org.
        raise MembershipError("You cannot change your own role.")

    current = db.fetch_one(
        "SELECT role FROM memberships WHERE org_id = %s AND user_id = %s",
        (org_id, user_id),
    )
    if current is None:
        raise MembershipError("That person is not a member of this organisation.")

    # Only an admin reaches this code at all (the route requires it), but the
    # last-admin rule still has to hold or an organisation can lock itself out.
    if current["role"] == "admin" and role != "admin" and admin_count(org_id) <= 1:
        raise MembershipError(
            "This is the last admin. Make someone else an admin first."
        )

    db.execute(
        "UPDATE memberships SET role = %s WHERE org_id = %s AND user_id = %s",
        (role, org_id, user_id),
    )
    return current["role"]


def remove_member(org_id: str, actor_id: str, actor_role: str, user_id: str) -> dict:
    if str(actor_id) == str(user_id):
        raise MembershipError("You cannot remove yourself.")

    target = db.fetch_one(
        """
        SELECT m.role, u.email
        FROM memberships m JOIN users u ON u.id = m.user_id
        WHERE m.org_id = %s AND m.user_id = %s
        """,
        (org_id, user_id),
    )
    if target is None:
        raise MembershipError("That person is not a member of this organisation.")

    if target["role"] == "admin" and admin_count(org_id) <= 1:
        raise MembershipError("This is the last admin and cannot be removed.")

    db.execute(
        "DELETE FROM memberships WHERE org_id = %s AND user_id = %s", (org_id, user_id)
    )
    # Their sessions for this org are now meaningless; drop them so access ends
    # immediately rather than whenever the cookie happens to expire.
    db.execute(
        "DELETE FROM sessions WHERE user_id = %s AND org_id = %s", (user_id, org_id)
    )
    return target


# --------------------------------------------------------------------------- #
# Invitations
# --------------------------------------------------------------------------- #
