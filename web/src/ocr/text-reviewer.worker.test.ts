import test from "node:test";
import assert from "node:assert/strict";
import { parseTextReviewerDecision } from "./text-reviewer-decision";

test("small reviewer parses strict and descriptive decisions", () => {
  assert.equal(parseTextReviewerDecision("TEXT"), true);
  assert.equal(parseTextReviewerDecision("NOISE"), false);
  assert.equal(
    parseTextReviewerDecision("The candidate is a random glyph."),
    false,
  );
  assert.equal(parseTextReviewerDecision("This is meaningful number."), true);
  assert.equal(
    parseTextReviewerDecision("The text is a meaningful formula"),
    true,
  );
});

test("small reviewer fails open when the tiny model only echoes input", () => {
  assert.equal(parseTextReviewerDecision("Candidate: Уварова"), null);
  assert.equal(parseTextReviewerDecision("The candidate is a 36"), null);
});
