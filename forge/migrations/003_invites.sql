-- Invitations.
--
-- An invite is a single-use token, not an account. Creating accounts on the
-- inviter's behalf would mean choosing a password for someone else and leaving
-- a live login sitting unused until they got round to it.

CREATE TABLE invites (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    email       text NOT NULL,
    role        text NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
    token_hash  text NOT NULL UNIQUE,
    invited_by  uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    accepted_at timestamptz,
    accepted_by uuid REFERENCES users(id) ON DELETE SET NULL
);

-- One live invite per email per org. A second invite to the same person should
-- replace the first, not race it.
CREATE UNIQUE INDEX invites_pending_idx
    ON invites (org_id, lower(email))
    WHERE accepted_at IS NULL;

CREATE INDEX invites_org_idx ON invites (org_id, created_at DESC);

-- 'owner' is deliberately absent from the CHECK above: ownership transfers are
-- a separate, deliberate act, not something handed out in an invitation.
