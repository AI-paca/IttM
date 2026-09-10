import assert from "node:assert/strict";
import test from "node:test";

import {
  buildNativePdfOracle,
  deserializeNativePdfOracle,
  serializeNativePdfOracle,
  type NativeExtractedSegment,
  type NativeTextObject,
} from "./pdf-native-oracle";
import { assembleSegmentTopologyHandoff } from "../ocr/segment-assembler";

const bbox = { left: 10, top: 90, right: 30, bottom: 100 };
const objects: NativeTextObject[] = [
  {
    objectId: "native-object-1",
    kind: "table",
    bbox,
    rows: [
      [0, 1],
      [2, 3],
    ],
    cells: [
      {
        row: 0,
        column: 0,
        rowSpan: 1,
        columnSpan: 1,
        bbox,
        candidateIndexes: [0],
      },
      {
        row: 0,
        column: 1,
        rowSpan: 1,
        columnSpan: 1,
        bbox,
        candidateIndexes: [1],
      },
      {
        row: 1,
        column: 0,
        rowSpan: 1,
        columnSpan: 1,
        bbox,
        candidateIndexes: [2],
      },
      {
        row: 1,
        column: 1,
        rowSpan: 1,
        columnSpan: 1,
        bbox,
        candidateIndexes: [3],
      },
    ],
  },
];
const segments: NativeExtractedSegment[] = ["A", "B", "C", "D"].map(
  (text, index) => ({
    segmentId: `native-segment-${index + 1}`,
    sourceItemIndex: index,
    bbox,
    topology: {
      object_id: "native-object-1",
      object_kind: "table",
      row: Math.floor(index / 2),
      column: index % 2,
      row_span: 1,
      column_span: 1,
    },
    text,
  }),
);

test("native PDF adapter delegates geometry to Rust and uses the shared assembler", () => {
  const assembler = {
    buildNativePdfParts: () => ({ objects, segments }),
    assembleTopology: assembleSegmentTopologyHandoff,
  };
  const oracle = buildNativePdfOracle([], assembler);
  assert.ok(oracle);
  assert.equal(oracle.nativeFindObject.objects[0].kind, "table");
  assert.equal(oracle.nativeSegmentExtraction.segments.length, 4);
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
  const assembler = {
    buildNativePdfParts: () => null,
    assembleTopology: assembleSegmentTopologyHandoff,
  };
  assert.equal(buildNativePdfOracle([], assembler), null);
});

test("native PDF artifacts reject an unrelated JSON shape", () => {
  assert.throws(
    () => deserializeNativePdfOracle('{"schema":"unrelated"}'),
    /Unsupported native PDF oracle artifact/,
  );
});
