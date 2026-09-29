-- Projects and their uploaded source files.
--
-- A project is one physical object a user wants modelled. It can carry several
-- assets - a photo from the front, another from the side, a PDF spec sheet - -- because more views resolve dimensions that no single image can.

CREATE TABLE projects (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    created_by  uuid REFERENCES users(id) ON DELETE SET NULL,
    name        text NOT NULL,
    category    text NOT NULL DEFAULT '',
    notes       text NOT NULL DEFAULT '',
    status      text NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'extracting', 'questions',
                                  'ready', 'building', 'complete', 'failed')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    archived_at timestamptz
);

CREATE INDEX projects_org_idx ON projects (org_id, created_at DESC);

CREATE TABLE assets (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id         uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    project_id     uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    uploaded_by    uuid REFERENCES users(id) ON DELETE SET NULL,

    original_name  text NOT NULL,
    -- What the browser claimed, and what the bytes actually are. They are
    -- stored separately because a mismatch is worth knowing about: it is how a
    -- renamed file, or a deliberately mislabelled one, shows up.
    declared_mime  text NOT NULL DEFAULT '',
    detected_mime  text NOT NULL
                   CHECK (detected_mime IN ('image/png', 'image/jpeg',
                                            'image/webp', 'application/pdf')),

    storage_key    text NOT NULL,
    bytes          bigint NOT NULL,
    -- Deduplicates re-uploads and gives the audit log something stable to
    -- point at when a file is discussed later.
    sha256         text NOT NULL,

    width          integer,
    height         integer,
    page_count     integer,
    has_text_layer boolean,

    role           text NOT NULL DEFAULT 'primary'
                   CHECK (role IN ('primary', 'additional_view', 'spec_sheet')),
    state          text NOT NULL DEFAULT 'uploaded'
                   CHECK (state IN ('uploaded', 'normalising', 'ready', 'failed')),
    error          text,

    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX assets_project_idx ON assets (project_id, created_at);
CREATE INDEX assets_org_idx ON assets (org_id, created_at DESC);

-- The same bytes uploaded twice into one project is a mistake, not a feature.
CREATE UNIQUE INDEX assets_project_sha_idx ON assets (project_id, sha256);
