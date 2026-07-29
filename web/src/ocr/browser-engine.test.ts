import test from "node:test";
import assert from "node:assert/strict";
import {
  createBrowserOcrProfile,
  shouldUseBrowserTableSlots,
} from "./browser-engine";
import { BROWSER_PIPELINE_PROFILES } from "./pipeline-config";

test("browser table slots run on vertically tiled document chunks", () => {
  const profile = createBrowserOcrProfile(
    null,
    BROWSER_PIPELINE_PROFILES.browser_tesseract_table_slots_t9,
  );

  assert.equal(
    shouldUseBrowserTableSlots(
      {
        input: new Blob(["chunk"]),
        index: 1,
        total: 4,
        width: 1200,
        height: 1800,
      },
      profile,
    ),
    true,
  );
});

test("browser table slots still respect disabled mode and pixel budget", () => {
  const profile = createBrowserOcrProfile(
    null,
    BROWSER_PIPELINE_PROFILES.browser_tesseract_table_slots_t9,
  );
  const disabled = { ...profile, tableSlotBuilder: "off" as const };

  assert.equal(
    shouldUseBrowserTableSlots(
      {
        input: new Blob(["chunk"]),
        index: 0,
        total: 3,
        width: 1200,
        height: 1800,
      },
      disabled,
    ),
    false,
  );
  assert.equal(
    shouldUseBrowserTableSlots(
      {
        input: new Blob(["chunk"]),
        index: 0,
        total: 3,
        width: profile.maxImagePixels + 1,
        height: 1,
      },
      profile,
    ),
    false,
  );
});
