import assert from "node:assert/strict";
import { test } from "node:test";
import {
  deriveTokens,
  interpolateWorkingScale,
  PURE_LIGHT,
  SOURCE_STICKER_SEEDS,
  VSCODE_LIGHT,
} from "./palettes";

function parseHex(hex: string): [number, number, number] {
  const n = parseInt(hex.replace("#", ""), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function hueFamily(hex: string): "green" | "other" {
  const [r, g, b] = parseHex(hex).map((v) => v / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const lit = (max + min) / 2;
  let hue = 0;
  let sat = 0;

  if (max !== min) {
    const delta = max - min;
    sat = lit > 0.5 ? delta / (2 - max - min) : delta / (max + min);
    switch (max) {
      case r:
        hue = ((g - b) / delta + (g < b ? 6 : 0)) * 60;
        break;
      case g:
        hue = ((b - r) / delta + 2) * 60;
        break;
      default:
        hue = ((r - g) / delta + 4) * 60;
    }
  }

  return sat * 100 >= 10 && hue >= 80 && hue < 170 ? "green" : "other";
}

function luminance(hex: string): number {
  const [r, g, b] = parseHex(hex).map((value) => {
    const channel = value / 255;
    return channel <= 0.03928
      ? channel / 12.92
      : Math.pow((channel + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

test("theme scale does not re-enter green after leaving it", () => {
  let wasGreen = false;
  let leftGreen = false;

  for (let i = 0; i <= 720; i++) {
    const family = hueFamily(interpolateWorkingScale(i / 720).accent_feedback);

    if (family === "green") {
      assert.equal(leftGreen, false, `green re-entered at sample ${i}`);
      wasGreen = true;
      continue;
    }

    if (wasGreen) {
      leftGreen = true;
    }
  }

  assert.equal(wasGreen, true, "expected one olive/green checkpoint");
});

test("theme background ramp has no abrupt luminance jump", () => {
  let previous = luminance(interpolateWorkingScale(0).base_background);
  let maxDelta = 0;

  for (let i = 1; i <= 720; i++) {
    const current = luminance(interpolateWorkingScale(i / 720).base_background);
    assert.ok(
      current + 0.002 >= previous,
      `background luminance went backwards at sample ${i}`,
    );
    maxDelta = Math.max(maxDelta, Math.abs(current - previous));
    previous = current;
  }

  assert.ok(maxDelta < 0.015, `background ramp is too sharp: ${maxDelta}`);
});

test("source sticker colors follow security tiers", () => {
  assert.equal(SOURCE_STICKER_SEEDS.browser, "#22A06B");
  assert.equal(SOURCE_STICKER_SEEDS.llm, "#D94A45");
  assert.equal(
    SOURCE_STICKER_SEEDS.local_tess,
    SOURCE_STICKER_SEEDS.local_easy,
  );
  assert.equal(SOURCE_STICKER_SEEDS.local_tess, SOURCE_STICKER_SEEDS.gateway);

  const tokens = deriveTokens(VSCODE_LIGHT);
  assert.equal(
    tokens["source-local-tess"],
    tokens["source-local-easy"],
    "same safety tier should produce same sticker color",
  );
  assert.equal(tokens["source-local-tess"], tokens["source-gateway"]);
  assert.notEqual(tokens["source-browser"], tokens["source-llm"]);
});

test("source sticker colors are fixed by safety hue but adapt to theme", () => {
  const workbenchTokens = deriveTokens(VSCODE_LIGHT);
  const pureLightTokens = deriveTokens(PURE_LIGHT);

  assert.notEqual(
    workbenchTokens["source-browser"],
    pureLightTokens["source-browser"],
  );
  assert.equal(
    pureLightTokens["source-local-tess"],
    pureLightTokens["source-local-easy"],
  );
  assert.equal(
    pureLightTokens["source-local-tess"],
    pureLightTokens["source-gateway"],
  );
});
