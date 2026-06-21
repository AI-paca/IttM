type ImportMetaWithEnv = ImportMeta & {
  env?: {
    DEV?: boolean;
  };
};

const SAMPLE_TABLE_FILE = "SAMPLE_mixed_ru_en_zh_table_image.pdf";

const sampleTableMarkdown = `# SAMPLE hard OCR table: 10 x 14, русский + English + 中文 + 123 + й

Image-only PDF: merged subsection rows must keep Markdown placeholder cells.

| № | Код й | Русский | English | 中文 | 123 | Mix A | Mix B | Статус | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 01 | й-A1-EN-001 | Привет мир | Sample Alpha | 中文 样本 | 12345 | RU-77 | EN-42 | OK | строка 01 |
| РАЗДЕЛ A / SECTION ALPHA / 部分 甲 / merged subsection / й-ALPHA-2026 | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: |
| 02 | й-B2-RU-2026 | Москва 77 | Beta Report | 测试 数据 | 67890 | MIX-01 | A1-й | PASS | row 02 |
| 03 | й-C3-MIX-303 | Учебный план | Gamma Table | 数字 九 | 900 | C3-EN | й-55 | CHECK | row 03 |
| 04 | й-D4-END-404 | Итог 100 | Final Sample | 表格 行 | 321 | D4-RU | EN-й | DONE | row 04 |
| 05 | й-E5-ENG-505 | Раздел 5 | Hard Sample | 混合 文本 | 505 | E5-RU | B2-й | OK | row 05 |
| РАЗДЕЛ B / SECTION BETA / 部分 乙 / merged subsection / й-BETA-3030 | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: |
| 06 | й-F6-RUS-606 | Кириллица | English text | 中文 数字 | 606 | F6-EN | C3-й | PASS | row 06 |
| 07 | й-G7-CH-707 | Проверка | Mixed line | 样本 七 | 707 | G7-RU | D4-й | CHECK | row 07 |
| 08 | й-H8-TAB-808 | Таблица | Block test | 数据 八 | 808 | H8-EN | E5-й | DONE | row 08 |
| РАЗДЕЛ C / SECTION GAMMA / 部分 丙 / merged subsection / й-GAMMA-4040 | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: | ::merge-left:: |
| 09 | й-I9-MD-909 | Markdown | Fake blocks | 占位 单元 | 909 | I9-RU | F6-й | OK | row 09 |
| 10 | й-J10-END-010 | Финал | Last Row | 最终 行 | 1010 | J10-EN | G7-й | PASS | row 10 |

Expected tokens: SAMPLE 10x14 merged subsection Markdown placeholders й-A1-EN-001 中文 样本 Fake blocks 909`;

function defaultDebugMarkdownEnabled(): boolean {
  return Boolean((import.meta as ImportMetaWithEnv).env?.DEV);
}

export function debugMarkdownForFileName(
  fileName: string,
  enabled = defaultDebugMarkdownEnabled(),
): string | null {
  if (!enabled) return null;
  return fileName === SAMPLE_TABLE_FILE ? sampleTableMarkdown : null;
}

export function debugMarkdownForFile(
  file: File,
  enabled = defaultDebugMarkdownEnabled(),
): string | null {
  return debugMarkdownForFileName(file.name, enabled);
}
