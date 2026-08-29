import assert from "node:assert/strict";
import test from "node:test";

import {
  buildNativePdfOracle as buildNativePdfOracleWithAssembler,
  deserializeNativePdfOracle,
  serializeNativePdfOracle,
} from "./pdf-native-oracle";
import { assembleSegmentTopologyHandoff } from "../ocr/segment-assembler";

const referenceAssembler = {
  assembleTopology: assembleSegmentTopologyHandoff,
};
const buildNativePdfOracle = (
  items: Parameters<typeof buildNativePdfOracleWithAssembler>[0],
) => buildNativePdfOracleWithAssembler(items, referenceAssembler);

test("native adapter owns bbox object detection and segment extraction", () => {
  const oracle = buildNativePdfOracle([
    { str: "A", width: 20, height: 10, transform: [1, 0, 0, 1, 10, 100] },
    { str: "B", width: 20, height: 10, transform: [1, 0, 0, 1, 80, 100] },
    { str: "C", width: 20, height: 10, transform: [1, 0, 0, 1, 10, 70] },
    { str: "D", width: 20, height: 10, transform: [1, 0, 0, 1, 80, 70] },
  ]);

  assert.ok(oracle);
  assert.equal(oracle.nativeFindObject.objects[0].kind, "table");
  assert.equal(oracle.nativeSegmentExtraction.segments.length, 4);
  assert.equal(oracle.assemblerHandoff.segments.length, 4);
  assert.equal(
    oracle.assembled.markdown,
    "| A | B |\n| --- | --- |\n| C | D |",
  );
  assert.deepEqual(
    deserializeNativePdfOracle(serializeNativePdfOracle(oracle)),
    oracle,
  );
});

test("image-only pages do not manufacture a native oracle", () => {
  assert.equal(buildNativePdfOracle([]), null);
  assert.equal(buildNativePdfOracle([{ str: "   " }]), null);
});

test("native bbox topology separates headers and preserves merged table rows", () => {
  const oracle = buildNativePdfOracle([
    { str: "Header", width: 60, height: 10, transform: [1, 0, 0, 1, 300, 140] },
    { str: "A", width: 20, height: 10, transform: [1, 0, 0, 1, 10, 110] },
    { str: "B", width: 20, height: 10, transform: [1, 0, 0, 1, 50, 110] },
    { str: "C", width: 20, height: 10, transform: [1, 0, 0, 1, 90, 110] },
    { str: "D", width: 20, height: 10, transform: [1, 0, 0, 1, 10, 90] },
    { str: "F", width: 20, height: 10, transform: [1, 0, 0, 1, 90, 90] },
    { str: "Section", width: 100, height: 10, transform: [1, 0, 0, 1, 10, 70] },
    { str: "G", width: 20, height: 10, transform: [1, 0, 0, 1, 10, 50] },
    { str: "H", width: 20, height: 10, transform: [1, 0, 0, 1, 50, 50] },
    { str: "I", width: 20, height: 10, transform: [1, 0, 0, 1, 90, 50] },
    { str: "Footer", width: 60, height: 10, transform: [1, 0, 0, 1, 300, 10] },
  ]);

  assert.ok(oracle);
  assert.deepEqual(
    oracle.nativeFindObject.objects.map((object) => object.kind),
    ["paragraph", "table", "paragraph"],
  );
  const table = oracle.nativeFindObject.objects[1];
  assert.equal(table.rows.length, 4);
  const merged = table.cells.find((cell) =>
    cell.candidateIndexes.some(
      (candidateIndex) =>
        oracle.nativeSegmentExtraction.segments.find(
          (segment) => segment.sourceItemIndex === candidateIndex,
        )?.text === "Section",
    ),
  );
  assert.ok(merged);
  assert.equal(merged.column, 0);
  assert.equal(merged.columnSpan, 3);
  assert.deepEqual(
    oracle.nativeSegmentExtraction.segments
      .filter((segment) => segment.text === "D" || segment.text === "F")
      .map((segment) => segment.topology.column),
    [0, 2],
  );
  assert.equal(oracle.nativeSegmentExtraction.segments.length, 11);
  assert.equal(new Set(oracle.nativeSegmentExtraction.segments.map((segment) => segment.segmentId)).size, 11);
});

test("overlapping glyph bboxes do not create native row spans", () => {
  const oracle = buildNativePdfOracle([
    { str: "A", width: 20, height: 16, transform: [1, 0, 0, 1, 10, 110] },
    { str: "B", width: 20, height: 16, transform: [1, 0, 0, 1, 90, 110] },
    { str: "C", width: 20, height: 16, transform: [1, 0, 0, 1, 10, 100] },
    { str: "D", width: 20, height: 16, transform: [1, 0, 0, 1, 90, 100] },
  ]);

  assert.ok(oracle);
  assert.ok(
    oracle.nativeSegmentExtraction.segments.every(
      (segment) => segment.topology.row_span === 1,
    ),
  );
});

test("native table continuation stays with the preceding table", () => {
  const oracle = buildNativePdfOracle([
    { str: "A", width: 20, height: 10, transform: [1, 0, 0, 1, 10, 150] },
    { str: "B", width: 20, height: 10, transform: [1, 0, 0, 1, 50, 150] },
    { str: "C", width: 20, height: 10, transform: [1, 0, 0, 1, 90, 150] },
    { str: "D", width: 20, height: 10, transform: [1, 0, 0, 1, 10, 130] },
    { str: "E", width: 20, height: 10, transform: [1, 0, 0, 1, 50, 130] },
    { str: "F", width: 20, height: 10, transform: [1, 0, 0, 1, 90, 130] },
    {
      str: "continued middle cell",
      width: 20,
      height: 10,
      transform: [1, 0, 0, 1, 50, 121],
    },
    { str: "G", width: 20, height: 10, transform: [1, 0, 0, 1, 10, 70] },
    { str: "H", width: 20, height: 10, transform: [1, 0, 0, 1, 50, 70] },
    { str: "I", width: 20, height: 10, transform: [1, 0, 0, 1, 90, 70] },
    { str: "J", width: 20, height: 10, transform: [1, 0, 0, 1, 10, 50] },
    { str: "K", width: 20, height: 10, transform: [1, 0, 0, 1, 50, 50] },
    { str: "L", width: 20, height: 10, transform: [1, 0, 0, 1, 90, 50] },
  ]);

  assert.ok(oracle);
  assert.deepEqual(
    oracle.nativeFindObject.objects.map((object) => object.kind),
    ["table", "table"],
  );
  const continuation = oracle.nativeSegmentExtraction.segments.find(
    (segment) => segment.text === "continued middle cell",
  );
  assert.ok(continuation);
  assert.equal(continuation.topology.object_id, "native-object-1");
  assert.equal(continuation.topology.column, 1);
  assert.equal(oracle.assembled.objects.length, 2);
  assert.ok(
    oracle.assembled.objects.every((object) =>
      object.cells.every(
        (cell) => cell.text === "" || cell.segment_ids.length > 0,
      ),
    ),
  );
});
