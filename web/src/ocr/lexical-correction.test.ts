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
      "AI-paca/IttM 7 merged",
      "Hw7 (SCA) Jun 26",
      "Hw5 (OCR engine, streaming, tests, debug area) Jun 19",
      "AI-paca/IttM 94 commits",
    ].join("\n"),
  );
});
