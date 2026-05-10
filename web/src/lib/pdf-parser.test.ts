import test from "node:test";
import assert from "node:assert/strict";
import {
  isNativePdfTextTrustworthy,
  mergeNativeAndOcrText,
} from "../ocr/pdf-text";

function hasUnsafeControlCharacters(text: string): boolean {
  return Array.from(text).some((char) => {
    const code = char.codePointAt(0) ?? 0;
    return code < 32 && char !== "\n" && char !== "\r" && char !== "\t";
  });
}

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

test("mergeNativeAndOcrText rejects corrupted native PDF text when OCR is readable", () => {
  const corruptNative = [
    "2026-05-02 02:02:05",
    '\u00109:\u0006\u0010\u0011:\u0005: \u0010 \u0007" "\u001f \u001b 9 "9\u001b\u0007\u001f\u001e\b@',
    '"\u001d\u001b\u0007* \u0006"\u001b\u0005 \u0007\u001b-\u001fC \u000f\u0010\u0011',
  ].join("\n");
  const ocrText = [
    "2026-05-02 02:02:05",
    "© 8 (800) 700-60-40",
    "Интегрированная система менеджмента сертифицирована",
  ].join("\n");

  const merged = mergeNativeAndOcrText(corruptNative, ocrText);

  assert.equal(merged, ocrText);
  assert.equal(hasUnsafeControlCharacters(merged), false);
});

test("isNativePdfTextTrustworthy rejects mojibake-like PDF text layer", () => {
  const corruptNative =
    "Ð\u0098Ð½Ñ\u0082ÐµÐ³Ñ\u0080Ð¸Ñ\u0080Ð¾Ð²Ð°Ð½Ð½Ð°Ñ\u008f Ñ\u0081Ð¸Ñ\u0081Ñ\u0082ÐµÐ¼Ð°";
  const ocrText = "Интегрированная система менеджмента";

  assert.equal(isNativePdfTextTrustworthy(corruptNative, ocrText), false);
});

test("mergeNativeAndOcrText rejects printable PDF text-layer garbage when OCR disagrees", () => {
  const corruptNative = [
    "Nr S",
    "TITTTO2O7 ©",
    "No я",
    '„0-0 ."0" 0 000000 0 ** 000000 /01\'$10231 (0 4/5657130 0*00 800000 .0000 -09000 0 :; :146 0" ; <<; 0<< =5',
    '-00000. — 00)000 0 ,п** @*90е #00000+ = 0 0 00- 0*"0000 000" DADB @ DOOA *00 .-0 D0 OOD',
  ].join("\n");
  const ocrText = [
    "чл",
    "© 8 (800) 700-60-40 (ILLIA OK: 1340}",
    "oH x",
    "J www.kdl.ru K Ham в couceTsx! NF% i",
    "Интегрированная система менеджмента сертифицирована Ha соответствие требованиям",
  ].join("\n");

  const merged = mergeNativeAndOcrText(corruptNative, ocrText);

  assert.equal(merged, ocrText);
  assert.doesNotMatch(merged, /TITTTO2O7|000000|DADB/);
});
