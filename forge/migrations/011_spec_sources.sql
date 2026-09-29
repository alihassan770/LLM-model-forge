-- Record which images a specification was read from, in the order the model
-- saw them.
--
-- A question's crop region says "x=0.4, y=0.6 of image 2". Resolving that back
-- to a picture needs to know what image 2 was, and with PDFs that is no longer
-- answerable from the asset list alone: one PDF contributes one image per page,
-- and which pages were chosen depends on what was on them.
--
-- Re-deriving it at crop time would mean re-running page selection on every
-- request and hoping it makes the same choice. Recording it once is cheaper
-- and cannot drift.
--
-- Shape, one entry per image, index = position in the array:
--   [{"asset_id": "...", "page": 3, "kind": "pdf_page", "label": "spec.pdf p3",
--     "storage_key": "org/uploads/asset/pages/0003@200dpi.png"},
--    {"asset_id": "...", "page": null, "kind": "image", "label": "photo.jpg",
--     "storage_key": "org/uploads/asset.jpg"}]

ALTER TABLE specs ADD COLUMN sources jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN specs.sources IS
  'Ordered list of the images this spec was read from; a question region''s '
  'image_index indexes into it.';
