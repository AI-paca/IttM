import test from "node:test";
import assert from "node:assert/strict";
import { applyBrowserLexicalCorrection } from "./lexical-correction";

test("browser t9 small cleans common UI OCR noise without table linting", () => {
  const text = [
    "[1 Created 94 commits in 1 repository ©",
    "Al-paca/IttM @ merged -¥.",
    "> Нм (SCA) jun 26",
    "> Нм (OCR engine, streaming, tests, debug area) Jun 19",
    "AI-pacallttM 94 commits",
  ].join("\n");

  assert.equal(
    applyBrowserLexicalCorrection(text, "t9_small"),
    [
      "Created 94 commits in 1 repository",
      "Al-paca/IttM @ merged",
      "Нм (SCA) Jun 26",
      "Нм (OCR engine, streaming, tests, debug area) Jun 19",
      "AI-pacallttM 94 commits",
    ].join("\n"),
  );
});

test("browser t9 small cleans technical commerce OCR confusables", () => {
  const text = [
    "| Product | FHO Display RAM 868 SSD 128GB DRS 19201080 |",
    "| Price | £474 | €259.99 |",
  ].join("\n");

  assert.equal(
    applyBrowserLexicalCorrection(text, "t9_small"),
    [
      "| Product | FHD Display RAM 8GB SSD 128GB DDR5 1920x1080 |",
      "| Price | €474 | €259.99 |",
    ].join("\n"),
  );
});

test("browser t9 small splits compact display resolutions without product-id rewrites", () => {
  const text = "HP Laptop 15440021s FHD Display 19201080 and panel 1024768";

  assert.equal(
    applyBrowserLexicalCorrection(text, "t9_small"),
    "HP Laptop 15440021s FHD Display 1920x1080 and panel 1024x768",
  );
});
