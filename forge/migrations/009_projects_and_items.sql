-- Introduce a level above the object being modelled.
--
-- Before: one project == one object. Photographing an office meant three
-- unrelated projects with nothing tying them together.
--
-- After:  project  "Office fit-out"
--           item   "Meeting table"   -> photos, spec, model, bill
--           item   "Desk chair"      -> photos, spec, model, bill
--           item   "Reception sofa"  -> photos, spec, model, bill
--
-- The *item* is what carries photos, specifications, builds and bills, because
-- those are all properties of one physical object. The project only groups
-- them and totals them up.
--
-- Existing data is preserved by promoting each current project to a project
-- containing exactly one item of the same name, so nothing is orphaned and no
-- URL's worth of work is lost.

CREATE TABLE items (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_by  uuid REFERENCES users(id) ON DELETE SET NULL,

    name        text NOT NULL,
    category    text NOT NULL DEFAULT '',
    notes       text NOT NULL DEFAULT '',
    status      text NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'extracting', 'questions',
                                  'ready', 'building', 'complete', 'failed')),
    -- Items are shown in a deliberate order rather than by creation time: a
    -- room schedule reads better as the client listed it.
    position    integer NOT NULL DEFAULT 0,

    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    archived_at timestamptz
);

CREATE INDEX items_project_idx ON items (project_id, position, created_at);
CREATE INDEX items_org_idx ON items (org_id, created_at DESC);

-- Promote every existing project to a project + one item.
INSERT INTO items (id, org_id, project_id, created_by, name, category, notes,
                   status, created_at, updated_at, archived_at)
SELECT gen_random_uuid(), org_id, id, created_by, name, category, notes,
       status, created_at, updated_at, archived_at
FROM projects;

-- Re-point the children at the item that now owns them.
ALTER TABLE assets    ADD COLUMN item_id uuid REFERENCES items(id) ON DELETE CASCADE;
ALTER TABLE specs     ADD COLUMN item_id uuid REFERENCES items(id) ON DELETE CASCADE;
ALTER TABLE jobs      ADD COLUMN item_id uuid REFERENCES items(id) ON DELETE CASCADE;
ALTER TABLE boms      ADD COLUMN item_id uuid REFERENCES items(id) ON DELETE CASCADE;
ALTER TABLE questions ADD COLUMN item_id uuid REFERENCES items(id) ON DELETE CASCADE;

UPDATE assets    c SET item_id = i.id FROM items i WHERE i.project_id = c.project_id;
UPDATE specs     c SET item_id = i.id FROM items i WHERE i.project_id = c.project_id;
UPDATE jobs      c SET item_id = i.id FROM items i WHERE i.project_id = c.project_id;
UPDATE boms      c SET item_id = i.id FROM items i WHERE i.project_id = c.project_id;
UPDATE questions c SET item_id = i.id FROM items i WHERE i.project_id = c.project_id;

ALTER TABLE assets    ALTER COLUMN item_id SET NOT NULL;
ALTER TABLE specs     ALTER COLUMN item_id SET NOT NULL;
ALTER TABLE jobs      ALTER COLUMN item_id SET NOT NULL;
ALTER TABLE boms      ALTER COLUMN item_id SET NOT NULL;
ALTER TABLE questions ALTER COLUMN item_id SET NOT NULL;

-- Indexes and uniqueness move with the parent. A spec revision is unique per
-- item, not per project - two items in one project each start at revision 1.
DROP INDEX IF EXISTS assets_project_sha_idx;
DROP INDEX IF EXISTS assets_project_idx;
DROP INDEX IF EXISTS specs_project_idx;
DROP INDEX IF EXISTS jobs_project_idx;
DROP INDEX IF EXISTS boms_project_idx;
DROP INDEX IF EXISTS questions_open_idx;

ALTER TABLE specs DROP CONSTRAINT IF EXISTS specs_project_id_revision_key;

CREATE UNIQUE INDEX assets_item_sha_idx ON assets (item_id, sha256);
CREATE INDEX assets_item_idx    ON assets (item_id, created_at);
CREATE UNIQUE INDEX specs_item_rev_idx ON specs (item_id, revision);
CREATE INDEX specs_item_idx     ON specs (item_id, revision DESC);
CREATE INDEX jobs_item_idx      ON jobs (item_id, created_at DESC);
CREATE INDEX boms_item_idx      ON boms (item_id, created_at DESC);
CREATE INDEX questions_open_idx ON questions (item_id) WHERE answered_at IS NULL;

-- project_id is now reachable through the item, so the duplicate would only
-- be another thing to keep in step.
ALTER TABLE assets    DROP COLUMN project_id;
ALTER TABLE specs     DROP COLUMN project_id;
ALTER TABLE jobs      DROP COLUMN project_id;
ALTER TABLE boms      DROP COLUMN project_id;
ALTER TABLE questions DROP COLUMN project_id;

-- The project keeps only what belongs to a grouping. Category and status
-- describe an object, and now live on the item.
ALTER TABLE projects DROP COLUMN category;
ALTER TABLE projects DROP COLUMN status;
ALTER TABLE projects ADD COLUMN client text NOT NULL DEFAULT '';
