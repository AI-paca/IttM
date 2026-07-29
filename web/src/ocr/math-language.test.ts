import assert from "node:assert/strict";
import test from "node:test";
import { scoreMathLanguage } from "./math-language";

test("browser math language scorer detects formulas", () => {
  const scores = scoreMathLanguage("x² + y² = 1");

  assert.ok(scores.equ > 0);
  assert.ok(scores.equ > scores.ell);
});

test("browser math language scorer detects units and percentages", () => {
  const unitScores = scoreMathLanguage("125.5 kg / m²");
  const percentScores = scoreMathLanguage("50%");

  assert.ok(unitScores.equ > unitScores.ell);
  assert.ok(unitScores.equ > 2);
  assert.ok(percentScores.equ > 0);
  assert.equal(percentScores.ell, 0);
});

test("browser math language scorer detects Greek formulas", () => {
  const scores = scoreMathLanguage("Σx_i = 2πr");

  assert.ok(scores.ell > 0);
  assert.ok(scores.equ > 0);
});

test("browser math language scorer avoids dates and catalog-like ids", () => {
  const dateScores = scoreMathLanguage("19.09.2017");
  const idScores = scoreMathLanguage("ID 15440021");

  assert.ok(dateScores.equ < 1);
  assert.equal(dateScores.ell, 0);
  assert.equal(idScores.equ, 0);
  assert.equal(idScores.ell, 0);
});

test("browser math language scorer recognizes compact math-like cells", () => {
  const compactFraction = scoreMathLanguage("2/3");
  const compactFormula = scoreMathLanguage("x+y");
  const compactGreek = scoreMathLanguage("π");

  assert.equal(compactFraction.equ > 1, true);
  assert.equal(compactFormula.equ > 0, true);
  assert.equal(compactGreek.ell > 0, true);
});
