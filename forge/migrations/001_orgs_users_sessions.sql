-- Phase 0: tenancy, identity, sessions, audit.
--
-- Multi-tenancy is here in the first migration rather than retrofitted later,
-- because every business table added after this one inherits the org_id
-- convention established now.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- --------------------------------------------------------------------------
-- Tenants
-- --------------------------------------------------------------------------
CREATE TABLE orgs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    slug        text NOT NULL UNIQUE,
    plan        text NOT NULL DEFAULT 'free',
    created_at  timestamptz NOT NULL DEFAULT now(),
    archived_at timestamptz
);

-- --------------------------------------------------------------------------
-- Identity
--
-- Users are global, not per-org: one person with one password can belong to
-- several client orgs. Which org a request acts on comes from the session.
-- --------------------------------------------------------------------------
CREATE TABLE users (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email             text NOT NULL UNIQUE,
    password_hash     text NOT NULL,
    name              text NOT NULL DEFAULT '',
    is_superadmin     boolean NOT NULL DEFAULT false,
    email_verified_at timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    last_login_at     timestamptz
);

-- Email is compared case-insensitively; the UNIQUE above is only a backstop
-- against two rows differing by case alone.
CREATE UNIQUE INDEX users_email_lower_idx ON users (lower(email));

CREATE TABLE memberships (
    org_id     uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       text NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    invited_by uuid REFERENCES users(id) ON DELETE SET NULL,
    joined_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, user_id)
);

CREATE INDEX memberships_user_idx ON memberships (user_id);

-- --------------------------------------------------------------------------
-- Sessions
--
-- Only the SHA-256 of the token is stored. A database leak therefore does not
-- hand out live sessions, and revocation is a DELETE rather than a token
-- blacklist we would have to maintain.
-- --------------------------------------------------------------------------
CREATE TABLE sessions (
    token_hash text PRIMARY KEY,
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id     uuid REFERENCES orgs(id) ON DELETE CASCADE,
    csrf_token text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    ip         text,
    user_agent text
);

CREATE INDEX sessions_user_idx ON sessions (user_id);
CREATE INDEX sessions_expiry_idx ON sessions (expires_at);

-- --------------------------------------------------------------------------
-- Audit
--
-- Append-only. Every authentication event, settings change, role change,
-- export and admin read lands here. org_id is nullable because some events
-- (a failed login for an unknown email) have no tenant yet.
-- --------------------------------------------------------------------------
CREATE TABLE audit_log (
    id            bigserial PRIMARY KEY,
    org_id        uuid REFERENCES orgs(id) ON DELETE SET NULL,
    actor_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    action        text NOT NULL,
    target_type   text,
    target_id     text,
    metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
    ip            text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX audit_org_time_idx ON audit_log (org_id, created_at DESC);
CREATE INDEX audit_action_time_idx ON audit_log (action, created_at DESC);
