-- Per-organisation configuration and secrets.
--
-- Two tables rather than one, because they have different security properties:
-- settings are readable by anyone who can see the org, secrets are encrypted at
-- rest and never leave the server. Mixing them would mean every settings read
-- risked exposing a key.

CREATE TABLE org_settings (
    org_id     uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    key        text NOT NULL,
    value      jsonb NOT NULL,
    updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, key)
);

-- Encrypted with Fernet under FORGE_SECRET_KEY. The plaintext never touches
-- this table, is never logged, and is never rendered back into a form field --
-- the UI shows only whether a value is set, and a masked hint.
CREATE TABLE org_secrets (
    org_id     uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    key        text NOT NULL,
    ciphertext bytea NOT NULL,
    hint       text NOT NULL DEFAULT '',
    updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, key)
);

-- Records every call to a model provider: which provider and model, what it
-- cost, how long it took, and how it ended. This is what makes the spend
-- dashboard and per-org billing possible, and it is written on failures too --
-- a provider outage is exactly when you want the history.
CREATE TABLE llm_calls (
    id              bigserial PRIMARY KEY,
    org_id          uuid REFERENCES orgs(id) ON DELETE CASCADE,
    actor_user_id   uuid REFERENCES users(id) ON DELETE SET NULL,
    provider        text NOT NULL,
    model           text NOT NULL,
    purpose         text NOT NULL,
    input_tokens    integer NOT NULL DEFAULT 0,
    output_tokens   integer NOT NULL DEFAULT 0,
    cached_tokens   integer NOT NULL DEFAULT 0,
    cost_usd        numeric(12, 6) NOT NULL DEFAULT 0,
    stop_reason     text,
    latency_ms      integer,
    ok              boolean NOT NULL DEFAULT true,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX llm_calls_org_time_idx ON llm_calls (org_id, created_at DESC);
