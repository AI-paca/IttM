# Rust dyadic table blocks experiment (2026-08-26)

## Diagnosis

For `doc_course_tasks_legacy.png`, `find-object` correctly finds one table object.
The previous Rust `separate-block` route then emitted one almost full-table OCR
crop (`row_span=9`, `column_span=1`). That is the first broken stage for this
fixture: language fallback cannot recover cells that were never represented as
overlapping blocks.

## Implemented planner

- Preserve detected horizontal and vertical table rules.
- Derive source cells from the actual grid instead of treating table rows as text
  lines.
- Detect a merged header row and emit it once as a standalone block.
- Partition remaining cells into tiles of at most `16 x 16` source segments.
- Emit one full-membership mask plus binary row and column masks for every tile.
- Pack only non-empty source-cell content into OCR slots.
- Preserve source pixel scale; compact packing moves segments but does not resize
  their glyphs.
- Keep at least two source segments in a block except for a tiny table of at most
  four cells.

For the 10-row by 6-column course-task table this produces one header block and
eight data masks. Every non-empty cell has a unique mask signature, so later
AND/OR/XOR reconstruction can recover individual segments.

## Visual evidence

Generated debug directory:

`debug/tmp/rust-dyadic-course-separate-v2`

Important artifacts:

- `03-find-object/overlay.png`
- `04-separate-block/blocks-contact.png`
- `04-separate-block/block-000.png` (merged header)
- `04-separate-block/block-002.png` (full-size compact data mask)
- `04-separate-block/manifest.json`

The full-size data mask is readable and contains no empty source cells. Visible
white space is internal padding around non-empty cell content, not omitted table
cells.

## Remaining work

This commit fixes planning and raster construction only. The ABI still needs an
explicit cell-to-mask membership map, and `get-segment` must use OCR token
positions plus mask signatures before final Markdown assembly. Until that is
implemented, concatenating all overlapping OCR block text would duplicate table
content.

## Get-segment update

ABI 6 now accepts OCR word text, confidence and crop-local bbox from both native
and browser adapters. Rust retains each compact slot placement, maps words back
to source table coordinates, merges repeated membership observations and
renders logical empty cells only after reconstruction. The text-only fallback
is preserved for engines that cannot provide word boxes.

Validation added: `table_word_boxes_map_back_to_source_cells`.
