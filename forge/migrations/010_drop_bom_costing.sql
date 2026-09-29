-- Remove costing from the materials list.
--
-- A price list belongs to a supplier and goes stale the week it is entered; a
-- cutting list does not. The list is now quantities and sizes only, so the
-- money columns are dropped rather than left to rot with figures nobody
-- maintains.
--
-- unit_price stays on `materials`: it costs nothing to keep and is where a
-- price would go if costing is ever wanted back.

ALTER TABLE bom_lines DROP COLUMN IF EXISTS unit_price;
ALTER TABLE bom_lines DROP COLUMN IF EXISTS line_total;

ALTER TABLE boms DROP COLUMN IF EXISTS total_cost;
ALTER TABLE boms DROP COLUMN IF EXISTS currency;
