-- Material catalogue and bills of materials.
--
-- Every organisation gets its own catalogue: prices, stock sizes and even
-- which timbers are available differ by supplier and country, so a single
-- global price list would be wrong for everyone.

CREATE TABLE materials (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    code          text NOT NULL,
    name          text NOT NULL,

    -- How the material is bought, which decides how a quantity is computed:
    --   linear  timber sold by length in stock sizes -> a cutting list
    --   sheet   ply/MDF sold in sheets               -> area, then sheets
    --   volume  foam sold by the cubic metre         -> volume
    --   area    fabric sold by the running metre     -> area + wastage
    --   unit    castors, brackets, screws            -> a count
    kind          text NOT NULL
                  CHECK (kind IN ('linear','sheet','volume','area','unit')),

    unit          text NOT NULL,
    density       numeric(10,2),        -- kg/m3, for weight estimates
    stock_length  numeric(10,2),        -- mm, for linear stock
    stock_width   numeric(10,2),        -- mm, for sheet stock
    stock_height  numeric(10,2),        -- mm, sheet thickness / bolt width
    waste_pct     numeric(5,2) NOT NULL DEFAULT 10,
    unit_price    numeric(12,4) NOT NULL DEFAULT 0,
    currency      text NOT NULL DEFAULT 'USD',
    active        boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),

    UNIQUE (org_id, code)
);

CREATE INDEX materials_org_idx ON materials (org_id, kind);

-- One bill per build, so a bill always corresponds to a model that exists and
-- to the exact spec revision it was derived from.
CREATE TABLE boms (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    job_id        uuid REFERENCES jobs(id) ON DELETE SET NULL,
    spec_revision integer NOT NULL,
    currency      text NOT NULL DEFAULT 'USD',
    total_cost    numeric(14,4) NOT NULL DEFAULT 0,
    total_weight  numeric(14,3),        -- kg
    advice        jsonb,                -- the model's notes, never its numbers
    created_by    uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX boms_project_idx ON boms (project_id, created_at DESC);

CREATE TABLE bom_lines (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    bom_id       uuid NOT NULL REFERENCES boms(id) ON DELETE CASCADE,
    org_id       uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,

    category     text NOT NULL,         -- timber, sheet, fabric, foam, hardware, fastener
    material_code text NOT NULL,
    description  text NOT NULL,
    part_ids     text[] NOT NULL DEFAULT '{}',

    quantity     numeric(14,3) NOT NULL,
    unit         text NOT NULL,
    -- Kept alongside the quantity so a line can be audited without recomputing:
    -- "4 lengths of 2400mm pine at 87% yield" is checkable, "4 lengths" is not.
    detail       jsonb,
    waste_pct    numeric(5,2) NOT NULL DEFAULT 0,
    unit_price   numeric(12,4) NOT NULL DEFAULT 0,
    line_total   numeric(14,4) NOT NULL DEFAULT 0,
    sort_order   integer NOT NULL DEFAULT 0
);

CREATE INDEX bom_lines_bom_idx ON bom_lines (bom_id, sort_order);
