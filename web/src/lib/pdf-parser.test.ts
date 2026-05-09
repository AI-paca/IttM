import test from "node:test";
import assert from "node:assert/strict";
import { mergeNativeAndOcrText } from "../ocr/pdf-text";

test("mergeNativeAndOcrText keeps native and unique image-layer OCR", () => {
  const merged = mergeNativeAndOcrText(
    "Invoice total 42",
    "Картинка OCR 中文测试",
  );

  assert.match(merged, /Invoice total 42/);
  assert.match(merged, /Картинка OCR 中文测试/);
});

test("mergeNativeAndOcrText drops exact duplicate OCR layer", () => {
  const merged = mergeNativeAndOcrText("Hello PDF 123", "Hello   PDF 123");

  assert.equal(merged, "Hello PDF 123");
});

test("mergeNativeAndOcrText keeps partial OCR additions", () => {
  const merged = mergeNativeAndOcrText("Hello PDF 123", "Hello PDF 123 EXTRA");

  assert.match(merged, /Hello PDF 123/);
  assert.match(merged, /EXTRA/);
});
