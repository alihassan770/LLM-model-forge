-- Forgotten passwords.
--
-- Only a SHA-256 of the token is stored, exactly as for sessions and invites:
-- the table is then useless to anyone who reads it, including us. The token
-- itself exists once, in the link that is sent, and nowhere else.
--
-- used_at rather than DELETE, so a link that has already been spent can be
-- told apart from one that never existed. "This link has already been used"
-- and "no such link" are different problems for the person holding it.

CREATE TABLE password_resets (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  text NOT NULL UNIQUE,

    -- Who asked. A self-service request is the user; an admin generating a
    -- link for a locked-out colleague is that admin, and that distinction
    -- belongs in the audit trail.
    requested_by uuid REFERENCES users(id) ON DELETE SET NULL,
    ip           text NOT NULL DEFAULT '',

    expires_at  timestamptz NOT NULL,
    used_at     timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX password_resets_user_idx ON password_resets (user_id, created_at DESC);

-- Live links only. Expired and spent rows are kept for the audit trail but
-- never need looking up.
CREATE INDEX password_resets_live_idx ON password_resets (expires_at)
    WHERE used_at IS NULL;
