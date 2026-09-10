import assert from "node:assert/strict";
import test from "node:test";

import {
  assembleSegmentTopologyHandoff,
  deserializeSegmentTopologyHandoff,
  SEGMENT_TOPOLOGY_HANDOFF_SCHEMA,
  serializeSegmentTopologyHandoff,
  type SegmentTopologyHandoff,
} from "./segment-assembler";

function handoff(
  source: SegmentTopologyHandoff["source"],
): SegmentTopologyHandoff {
  return {
    schema: SEGMENT_TOPOLOGY_HANDOFF_SCHEMA,
    source,
    segments: [
      {
        segment_id: "s1",
        topology: {
          object_id: "table-1",
          object_kind: "table",
          row: 0,
          column: 0,
          row_span: 1,
          column_span: 1,
        },
        text: "A",
      },
      {
        segment_id: "s2",
        topology: {
          object_id: "table-1",
          object_kind: "table",
          row: 0,
          column: 1,
          row_span: 1,
          column_span: 1,
        },
        text: "B",
      },
      {
        segment_id: "s3",
        topology: {
          object_id: "table-1",
          object_kind: "table",
          row: 1,
          column: 0,
          row_span: 1,
          column_span: 1,
        },
        text: "C",
      },
      {
        segment_id: "s4",
        topology: {
          object_id: "table-1",
          object_kind: "table",
          row: 1,
          column: 1,
          row_span: 1,
          column_span: 1,
        },
        text: "D",
      },
    ],
  };
}

test("native and raster adapters share only the post-segment assembler", () => {
  const native = assembleSegmentTopologyHandoff(
    handoff("trusted_pdf_text_layer"),
  );
  const raster = assembleSegmentTopologyHandoff(handoff("raster_geometry"));
  assert.equal(native.markdown, "| A | B |\n| --- | --- |\n| C | D |");
  assert.equal(raster.markdown, native.markdown);
});

test("table markdown formats only complete numeric ratio cells", () => {
  const value = handoff("raster_geometry");
  value.segments[0].text = "7/8";
  value.segments[1].text = "docs/7/8";
  value.segments[2].text = "2026/07/31";
  value.segments[3].text = "7/8%";

  const artifact = assembleSegmentTopologyHandoff(value);

  assert.equal(artifact.objects[0].cells[0].text, "7/8");
  assert.equal(
    artifact.markdown,
    ["| 7 / 8 | docs/7/8 |", "| --- | --- |", "| 2026/07/31 | 7/8% |"].join(
      "\n",
    ),
  );
});

test("segment topology handoff is restartable", () => {
  const value = handoff("raster_geometry");
  assert.deepEqual(
    deserializeSegmentTopologyHandoff(serializeSegmentTopologyHandoff(value)),
    value,
  );
});

test("assembler keeps sparse cells while rendering the complete logical shape", () => {
  const value: SegmentTopologyHandoff = {
    schema: SEGMENT_TOPOLOGY_HANDOFF_SCHEMA,
    source: "trusted_pdf_text_layer",
    object_layouts: [
      {
        object_id: "table-sparse",
        object_kind: "table",
        logical_row_count: 4,
        logical_column_count: 3,
      },
    ],
    segments: [
      {
        segment_id: "header-1",
        topology: {
          object_id: "table-sparse",
          object_kind: "table",
          row: 0,
          column: 0,
          row_span: 1,
          column_span: 3,
        },
        text: "Merged",
      },
      {
        segment_id: "header-2",
        topology: {
          object_id: "table-sparse",
          object_kind: "table",
          row: 0,
          column: 0,
          row_span: 1,
          column_span: 3,
        },
        text: "header",
      },
      {
        segment_id: "left",
        topology: {
          object_id: "table-sparse",
          object_kind: "table",
          row: 1,
          column: 0,
          row_span: 1,
          column_span: 1,
        },
        text: "Left",
      },
      {
        segment_id: "right",
        topology: {
          object_id: "table-sparse",
          object_kind: "table",
          row: 1,
          column: 2,
          row_span: 1,
          column_span: 1,
        },
        text: "Right",
      },
      {
        segment_id: "middle",
        topology: {
          object_id: "table-sparse",
          object_kind: "table",
          row: 2,
          column: 1,
          row_span: 1,
          column_span: 1,
        },
        text: "Middle",
      },
    ],
  };

  const artifact = assembleSegmentTopologyHandoff(value);
  assert.equal(artifact.objects[0].cells.length, 4);
  assert.equal(artifact.objects[0].logical_row_count, 4);
  assert.equal(artifact.objects[0].logical_column_count, 3);
  assert.ok(
    artifact.objects[0].cells.every((cell) => cell.segment_ids.length > 0),
  );
  assert.deepEqual(artifact.objects[0].cells[0].segment_ids, [
    "header-1",
    "header-2",
  ]);
  assert.equal(
    artifact.markdown,
    [
      "| Merged header |  |  |",
      "| --- | --- | --- |",
      "| Left |  | Right |",
      "|  | Middle |  |",
      "|  |  |  |",
    ].join("\n"),
  );
});

test("assembler validates and resolves compact OCR evidence references", () => {
  const value = handoff("raster_geometry");
  value.evidence_index = { ocr_job_ids: ["job-a", "job-b"] };
  value.segments[0].source_segment_ids = ["source-a"];
  value.segments[0].evidence = { ocr_job_refs: [0] };
  value.segments[1].evidence = { ocr_job_refs: [1] };

  assert.deepEqual(
    deserializeSegmentTopologyHandoff(serializeSegmentTopologyHandoff(value)),
    value,
  );
  assert.throws(
    () =>
      deserializeSegmentTopologyHandoff(
        JSON.stringify({
          ...value,
          segments: [{ ...value.segments[0], evidence: { ocr_job_refs: [2] } }],
        }),
      ),
    /Invalid evidence/,
  );
});

test("assembler clips inferred spans around stronger anchors", () => {
  const value: SegmentTopologyHandoff = {
    schema: SEGMENT_TOPOLOGY_HANDOFF_SCHEMA,
    source: "raster_geometry",
    segments: [
      {
        segment_id: "merged",
        topology: {
          object_id: "table-overlap",
          object_kind: "table",
          row: 0,
          column: 0,
          row_span: 1,
          column_span: 2,
        },
        text: "Merged",
      },
      {
        segment_id: "overlap",
        topology: {
          object_id: "table-overlap",
          object_kind: "table",
          row: 0,
          column: 1,
          row_span: 1,
          column_span: 1,
        },
        text: "Overlap",
      },
    ],
  };

  const artifact = assembleSegmentTopologyHandoff(value);
  assert.deepEqual(
    artifact.objects[0].cells.map((cell) => ({
      text: cell.text,
      row_span: cell.row_span,
      column_span: cell.column_span,
    })),
    [
      { text: "Merged", row_span: 1, column_span: 1 },
      { text: "Overlap", row_span: 1, column_span: 1 },
    ],
  );
});

test("trusted native topology is not rewritten from paragraph word count", () => {
  const value: SegmentTopologyHandoff = {
    schema: SEGMENT_TOPOLOGY_HANDOFF_SCHEMA,
    source: "trusted_pdf_text_layer",
    segments: [
      {
        segment_id: "paragraph",
        topology: {
          object_id: "paragraph-1",
          object_kind: "paragraph",
          row: 0,
          column: 0,
          row_span: 1,
          column_span: 1,
        },
        text: "three word header",
      },
      ...["A", "B", "C"].map((text, column) => ({
        segment_id: `table-${column}`,
        topology: {
          object_id: "table-1",
          object_kind: "table" as const,
          row: 0,
          column,
          row_span: 1,
          column_span: 1,
        },
        text,
      })),
    ],
  };

  const artifact = assembleSegmentTopologyHandoff(value);
  assert.equal(artifact.objects.length, 2);
  assert.equal(artifact.objects[0].kind, "paragraph");
  assert.equal(artifact.objects[1].kind, "table");
  assert.ok(
    artifact.objects.every((object) =>
      object.cells.every(
        (cell) => cell.text === "" || cell.segment_ids.length > 0,
      ),
    ),
  );
});
