#!/usr/bin/env node

import { readFile } from "node:fs/promises";
import {
  assembleSegmentTopologyHandoff,
  deserializeSegmentTopologyHandoff,
} from "../../web/src/ocr/segment-assembler";

const input = process.argv[2];
if (!input) {
  console.error(
    "Usage: assemble-segment-topology-handoff.ts HANDOFF.json",
  );
  process.exit(2);
}

const handoff = deserializeSegmentTopologyHandoff(
  await readFile(input, "utf8"),
);
const artifact = assembleSegmentTopologyHandoff(handoff);
process.stdout.write(`${JSON.stringify(artifact)}\n`);
