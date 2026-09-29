-- Extracted part specifications.
--
-- Append-only: every extraction and every answered question produces a new
-- revision rather than overwriting the last. A user can then see exactly what
-- changed and when, and roll back - which matters because a spec is the thing
-- the geometry and the bill of materials are both derived from.

CREATE TABLE specs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision        integer NOT NULL,
    body            jsonb NOT NULL,
    parent_revision integer,
    created_by      uuid REFERENCES users(id) ON DELETE SET NULL,

    provider        text NOT NULL DEFAULT '',
    model           text NOT NULL DEFAULT '',
    input_tokens    integer NOT NULL DEFAULT 0,
    output_tokens   integer NOT NULL DEFAULT 0,
    cost_usd        numeric(12, 6) NOT NULL DEFAULT 0,

    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, revision)
);

CREATE INDEX specs_project_idx ON specs (project_id, revision DESC);

-- The questions raised by an extraction, and the answers given to them.
CREATE TABLE questions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    spec_id       uuid NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
    project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,

    part_id       text NOT NULL DEFAULT '',
    dimension     text NOT NULL,
    question_text text NOT NULL,
    why_needed    text NOT NULL DEFAULT '',
    unit          text NOT NULL DEFAULT 'mm',
    suggested_min numeric,
    suggested_max numeric,
    blocking      boolean NOT NULL DEFAULT true,

    answer_value  numeric,
    answer_unit   text,
    answered_by   uuid REFERENCES users(id) ON DELETE SET NULL,
    answered_at   timestamptz,

    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX questions_spec_idx ON questions (spec_id, blocking DESC, created_at);
CREATE INDEX questions_open_idx ON questions (project_id)
    WHERE answered_at IS NULL;
