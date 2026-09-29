-- Build jobs and the files they produce.
--
-- A job row exists even when the build runs inline, because the history is
-- what makes a bad model explainable: which spec revision, which Blender, how
-- long it took, what it wrote, and what the log said if it failed.

CREATE TABLE jobs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    spec_id       uuid REFERENCES specs(id) ON DELETE SET NULL,
    spec_revision integer,

    kind          text NOT NULL DEFAULT 'build',
    state         text NOT NULL DEFAULT 'queued'
                  CHECK (state IN ('queued','running','done','failed','cancelled')),

    requested_by  uuid REFERENCES users(id) ON DELETE SET NULL,
    claimed_by    text,
    attempts      integer NOT NULL DEFAULT 0,

    -- What actually ran, so a build can be reproduced or blamed accurately.
    engine        text NOT NULL DEFAULT '',
    objects_built integer,
    duration_ms   integer,
    result        jsonb,
    error         text,
    log           text,

    created_at    timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    finished_at   timestamptz,
    heartbeat_at  timestamptz
);

CREATE INDEX jobs_project_idx ON jobs (project_id, created_at DESC);
-- The claim index: a partial index on exactly the rows a worker competes for.
CREATE INDEX jobs_queued_idx ON jobs (created_at) WHERE state = 'queued';

CREATE TABLE artifacts (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    job_id      uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    format      text NOT NULL,
    filename    text NOT NULL,
    storage_key text NOT NULL,
    bytes       bigint NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX artifacts_job_idx ON artifacts (job_id);
CREATE UNIQUE INDEX artifacts_job_format_idx ON artifacts (job_id, format);
