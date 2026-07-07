import test from "node:test";
import assert from "node:assert/strict";
import { alignedNumericTextToMarkdown } from "./aligned-rows";

test("builds a ranked table from browser OCR text", () => {
  const result = alignedNumericTextToMarkdown(`Global Top 3 devices
Data Source: benchmark
1 Alpha Phone 1863133
2 Beta Phone 1532816
8 Gamma Phone 1466153`);

  assert.ok(result);
  assert.equal(result.rows, 3);
  assert.equal(result.cols, 3);
  assert.match(result.markdown, /\| Rank \| Item \| Score \|/);
  assert.match(result.markdown, /\| 2 \| Beta Phone \| 1532816 \|/);
  assert.match(result.markdown, /\| 3 \| Gamma Phone \| 1466153 \|/);
});

test("splits phone benchmark rows from browser OCR text", () => {
  const result =
    alignedNumericTextToMarkdown(`Global Top 7 Best Performing Sub-flagship Phones, November 2025
Data Source: Antutu Benchmark V11 М Е.
1 PocoX7 Pro Dimensity8400-Ultra 12GB+512GB 1863133
2 PocoX6Pro5G Dimensity8300-Ultra 12GB+512GB 1532816
4 InfinixGT20Pro_Dimensity8200 Ultimate 12GB+256GB 1301823
5 OnePlusNord4 snapdragon7+Gen3 8GB+256GB 1276239
6 РосоЕБ Snapdragon7+Gen2 12GB+256GB 1252520
7 1000 29 snapdragon7Gen3 8GB+256GB 1011022
9 Motorola Edge 60 Fusion Dimensity7300-Utra 868+25668 881908`);

  assert.ok(result);
  assert.equal(result.rows, 7);
  assert.equal(result.cols, 5);
  assert.match(result.markdown, /\*average score/);
  assert.doesNotMatch(result.markdown, /М Е\./);
  assert.match(
    result.markdown,
    /\| Rank \| Model \| Chipset \| Memory \| Score \|/,
  );
  assert.match(
    result.markdown,
    /\| 1 \| Poco X7 Pro \| Dimensity 8400-Ultra \| 12GB\+512GB \| 1863133 \|/,
  );
  assert.match(
    result.markdown,
    /\| 3 \| Infinix GT 20 Pro \| Dimensity 8200 Ultimate \| 12GB\+256GB \| 1301823 \|/,
  );
  assert.match(
    result.markdown,
    /\| 4 \| OnePlus Nord 4 \| Snapdragon 7\+ Gen 3 \| 8GB\+256GB \| 1276239 \|/,
  );
  assert.match(
    result.markdown,
    /\| 5 \| Poco F5 \| Snapdragon 7\+ Gen 2 \| 12GB\+256GB \| 1252520 \|/,
  );
  assert.match(
    result.markdown,
    /\| 6 \| iQOO Z9 \| Snapdragon 7 Gen 3 \| 8GB\+256GB \| 1011022 \|/,
  );
  assert.match(
    result.markdown,
    /\| 7 \| Motorola Edge 60 Fusion \| Dimensity 7300-Ultra \| 8GB\+256GB \| 881908 \|/,
  );
});

test("builds a two-column name and value table", () => {
  const result = alignedNumericTextToMarkdown(`Alpha 7
Beta 12
Gamma 3`);

  assert.ok(result);
  assert.equal(result.cols, 2);
  assert.match(result.markdown, /\| Item \| Value \|/);
  assert.match(result.markdown, /\| Gamma \| 3 \|/);
});

test("keeps first visual row as header for long name score lists", () => {
  const result = alignedNumericTextToMarkdown(`KagTaeB  -
Кононюк 6
Кудрявцева 7
Кузьмина | 10
Малафеева 6
Мелега 8
Новеньков |7
Родионов 4
Тощевиков | 9
'Федько 6
Чапурин 5
Шубин 8`);

  assert.ok(result);
  assert.equal(result.rows, 12);
  assert.equal(result.cols, 2);
  assert.match(result.markdown, /^\| Кавтаев \| - \|/);
  assert.match(result.markdown, /\| Кузьмина \| 10 \|/);
  assert.match(result.markdown, /\| Федько \| 6 \|/);
});

test("keeps unstructured browser OCR as plain text", () => {
  assert.equal(
    alignedNumericTextToMarkdown(`Report 2025
Page 1`),
    null,
  );
});
