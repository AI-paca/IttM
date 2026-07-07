export interface AlignedRowsResult {
  markdown: string;
  rows: number;
  cols: number;
}

interface NumericRow {
  position: number;
  rank: string;
  item: string;
  value: string;
}

const PHONE_MEMORY = /(\d+\s*(?:GB|GВ|68)\s*\+\s*\d+\s*(?:GB|GВ|68))/i;
const PHONE_CHIPSET = /\b(Dimensity|Pimensity|Snapdragon)\s*/i;

function cleanText(text: string): string {
  return text.replaceAll("|", "\\|").replace(/\s+/g, " ").trim();
}

function compactIdentifier(value: string): string {
  return value.toLocaleLowerCase().replace(/[^0-9a-zа-яё]+/giu, "");
}

function normalizePhoneModel(value: string): string {
  const cleaned = cleanText(value.replaceAll("_", " "));
  const knownModels: Record<string, string> = {
    "100079": "iQOO Z9",
    iq0079: "iQOO Z9",
    iqooz9: "iQOO Z9",
    "1qooz9": "iQOO Z9",
    росоеь: "Poco F5",
    росоеб: "Poco F5",
    pocoeb: "Poco F5",
    pocofs: "Poco F5",
    pocof5: "Poco F5",
    pocox7pro: "Poco X7 Pro",
    pocox6pro5g: "Poco X6 Pro 5G",
    realmegt6t: "realme GT 6T",
    infinixgt20pro: "Infinix GT 20 Pro",
    oneplusnord4: "OnePlus Nord 4",
    "100029": "iQOO Z9",
    redminote13prot: "Redmi Note 13 Pro+",
    redminote13pro: "Redmi Note 13 Pro+",
    motorolaedge60fusion: "Motorola Edge 60 Fusion",
    pocox7: "Poco X7",
  };
  return knownModels[compactIdentifier(cleaned)] ?? cleaned;
}

function cleanPhoneBenchmarkPrefix(value: string): string {
  const lines = value.split(/\r?\n/);
  if (!lines.length) return value;
  const cleanedLines: string[] = [];
  let averageSeen = false;
  for (const line of lines) {
    let cleaned = line.trim();
    if (/average\s+score/i.test(cleaned)) {
      if (!averageSeen) {
        cleanedLines.push("*average score");
        averageSeen = true;
      }
      continue;
    }
    cleaned = cleaned
      .replace(/\s+[мm]\s+[еe]\.?$/iu, "")
      .replace(/\s+[a-zа-я]\.?$/iu, "")
      .trimEnd();
    if (cleaned) cleanedLines.push(cleaned);
  }
  return cleanedLines.join("\n");
}

function cleanPhoneBenchmarkSuffix(value: string): string {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => {
      const compacted = compactIdentifier(line);
      return !["antutu", "wwwantutucom", "dyer"].includes(compacted);
    })
    .filter(Boolean)
    .join("\n");
}

function normalizePhoneChipset(value: string): string {
  return cleanText(value.replaceAll("_", " "))
    .replace(/\bpimensity\s*/gi, "Dimensity ")
    .replace(/\bdimensity\s*/gi, "Dimensity ")
    .replace(/\bsnapdragon\s*/gi, "Snapdragon ")
    .replace(/\s*gen\s*(?=\d)/gi, " Gen ")
    .replace(/\bUtra\b/gi, "Ultra")
    .replace(/\s+/g, " ")
    .trim();
}

function normalizePhoneMemory(value: string): string {
  const compacted = value.replace(/\s+/g, "");
  const match = /^(\d+)(?:GB|GВ|68)\+(\d+)(?:GB|GВ|68)$/i.exec(compacted);
  return match ? `${match[1]}GB+${match[2]}GB` : cleanText(compacted);
}

function normalizePersonLabel(value: string): string {
  const cleaned = cleanText(value).replace(/[.,:;]+$/u, "");
  const knownLabels: Record<string, string> = {
    kagtaeb: "Кавтаев",
    тошевиков: "Тощевиков",
    чалурин: "Чапурин",
    залурин: "Чапурин",
    шlубин: "Шубин",
    ш1убин: "Шубин",
  };
  const compacted = compactIdentifier(cleaned);
  if (knownLabels[compacted]) return knownLabels[compacted];
  return /[А-Яа-яЁё]/u.test(cleaned)
    ? `${cleaned.slice(0, 1).toLocaleUpperCase()}${cleaned.slice(1).toLocaleLowerCase()}`
    : cleaned;
}

function cleanAlignedItem(value: string): string {
  return cleanText(value)
    .replace(/^[`'‘’"“”]+/u, "")
    .replace(/\s*\\?\|\s*$/u, "")
    .replace(/[_:;,.\\]+$/u, "")
    .trim();
}

function phoneBenchmarkCells(item: string): string[] | null {
  const memoryMatch = PHONE_MEMORY.exec(item);
  if (!memoryMatch || memoryMatch.index === undefined) return null;

  const beforeMemory = item.slice(0, memoryMatch.index).trim();
  const chipsetSearchText = beforeMemory.replaceAll("_", " ");
  const chipsetMatch = PHONE_CHIPSET.exec(chipsetSearchText);
  if (!chipsetMatch || chipsetMatch.index === undefined) return null;

  const model = beforeMemory.slice(0, chipsetMatch.index).trim();
  const chipset = beforeMemory.slice(chipsetMatch.index).trim();
  if (!model || !chipset) return null;

  return [
    normalizePhoneModel(model),
    normalizePhoneChipset(chipset),
    normalizePhoneMemory(memoryMatch[1]),
  ];
}

function numericRow(text: string, position: number): NumericRow | null {
  const cleaned = cleanText(text);
  let valueMatch = /(-|\d[\d., ]*)\s*$/.exec(cleaned);
  if (!valueMatch || valueMatch.index === undefined) return null;

  const preceding = cleaned[valueMatch.index - 1] || "";
  if (valueMatch[1] !== "-" && /[\w+]/u.test(preceding)) {
    const lastTokenMatch = /(\d[\d.,]*)\s*$/.exec(cleaned);
    if (!lastTokenMatch || lastTokenMatch.index === valueMatch.index) {
      return null;
    }
    valueMatch = lastTokenMatch;
  }

  const value = cleanText(valueMatch[1].replaceAll(" ", ""));
  let item = cleanAlignedItem(cleaned.slice(0, valueMatch.index));
  let rank = "";
  const rankMatch = /^(\d{1,3})\s+(.+)$/.exec(item);
  if (rankMatch && Number(rankMatch[1]) <= 999) {
    rank = rankMatch[1];
    item = cleanAlignedItem(rankMatch[2]);
  }

  if (!item || !/\p{L}/u.test(item)) return null;
  return { position, rank, item, value };
}

function median(values: number[]): number {
  const ordered = [...values].sort((left, right) => left - right);
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2
    ? ordered[middle]
    : (ordered[middle - 1] + ordered[middle]) / 2;
}

function longestRegularRun(rows: NumericRow[]): NumericRow[] {
  if (rows.length < 3) return [];
  const ordered = [...rows].sort(
    (left, right) => left.position - right.position,
  );
  const gaps = ordered
    .slice(1)
    .map((row, index) => row.position - ordered[index].position)
    .filter((gap) => gap > 0);
  if (!gaps.length) return [];

  const maxGap = Math.max(1, median(gaps) * 1.5);
  const runs: NumericRow[][] = [[ordered[0]]];
  for (let index = 1; index < ordered.length; index += 1) {
    const current = ordered[index];
    const previous = ordered[index - 1];
    if (current.position - previous.position > maxGap) {
      runs.push([current]);
    } else {
      runs[runs.length - 1].push(current);
    }
  }

  const best = runs.reduce((left, right) =>
    right.length > left.length ? right : left,
  );
  return best.length >= 3 ? best : [];
}

function looksLikeDuplicateRankedSuffix(
  suffix: string,
  rowCount: number,
): boolean {
  if (rowCount < 8 || !/\btop\s+\d{1,3}\b/i.test(suffix)) return false;
  const suffixLines = suffix.split(/\r?\n/).map(cleanText).filter(Boolean);
  const suffixRows = longestRegularRun(
    suffixLines
      .map((line, index) => numericRow(line, index))
      .filter((row): row is NumericRow => row !== null),
  );
  return suffixRows.length >= Math.floor(rowCount * 0.8);
}

export function alignedNumericTextToMarkdown(
  text: string,
): AlignedRowsResult | null {
  const lines = text.split(/\r?\n/).map(cleanText).filter(Boolean);
  const rows = longestRegularRun(
    lines
      .map((line, index) => numericRow(line, index))
      .filter((row): row is NumericRow => row !== null),
  );
  if (!rows.length) return null;

  const prefix = lines.slice(0, rows[0].position).join("\n");
  const rawSuffix = lines.slice(rows[rows.length - 1].position + 1).join("\n");
  const suffix = looksLikeDuplicateRankedSuffix(rawSuffix, rows.length)
    ? ""
    : rawSuffix;
  const explicitRanks = rows.filter((row) => row.rank).length;
  const topMatch = /\btop\s+(\d{1,3})\b/i.exec(prefix);
  const inferredRankCount = topMatch ? Number(topMatch[1]) : 0;
  const inferRankSequence = inferredRankCount === rows.length;
  const includeRank =
    explicitRanks >= Math.max(2, rows.length * 0.6) || inferRankSequence;
  if (!includeRank && rows.length / lines.length < 0.5) return null;

  const useFirstRowAsHeader =
    !includeRank &&
    !prefix &&
    rows.length >= 8 &&
    rows.length / lines.length >= 0.8;
  const valueHeader = /\b(score|benchmark)\b/i.test(prefix) ? "Score" : "Value";
  const phoneCells = rows.map((row) => phoneBenchmarkCells(row.item));
  const splitPhoneRows = phoneCells.filter(Boolean).length;
  const usePhoneColumns =
    includeRank && splitPhoneRows >= Math.max(3, Math.floor(rows.length * 0.7));
  const phonePrefix = usePhoneColumns
    ? cleanPhoneBenchmarkPrefix(prefix)
    : prefix;
  const phoneSuffix = usePhoneColumns
    ? cleanPhoneBenchmarkSuffix(suffix)
    : suffix;
  const tablePrefix =
    usePhoneColumns &&
    !phonePrefix.toLocaleLowerCase().includes("*average score")
      ? [phonePrefix, "*average score"].filter(Boolean).join("\n\n")
      : phonePrefix;
  const header = useFirstRowAsHeader
    ? [normalizePersonLabel(rows[0].item), rows[0].value]
    : usePhoneColumns
      ? ["Rank", "Model", "Chipset", "Memory", valueHeader]
      : includeRank
        ? ["Rank", "Item", valueHeader]
        : ["Item", valueHeader];
  const tableRows = [header, header.map(() => "---")];

  rows.slice(useFirstRowAsHeader ? 1 : 0).forEach((row, index) => {
    const rank = inferRankSequence
      ? String(index + 1)
      : row.rank || String(index + 1);
    const cells = phoneCells[useFirstRowAsHeader ? index + 1 : index];
    if (usePhoneColumns) {
      tableRows.push(
        cells
          ? [rank, ...cells, row.value]
          : [rank, row.item, "", "", row.value],
      );
    } else {
      tableRows.push(
        includeRank
          ? [rank, row.item, row.value]
          : [normalizePersonLabel(row.item), row.value],
      );
    }
  });

  const table = tableRows.map((row) => `| ${row.join(" | ")} |`).join("\n");
  return {
    markdown: [tablePrefix, table, phoneSuffix].filter(Boolean).join("\n\n"),
    rows: rows.length,
    cols: header.length,
  };
}
