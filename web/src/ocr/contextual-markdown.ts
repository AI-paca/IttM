import { mergeOcrTextChunks } from "./merge-ocr-chunks";

function compact(text: string): string {
  return text.toLocaleLowerCase().replace(/[^0-9a-zа-яё]+/gu, "");
}

function looksLikeCourtStatsSlide(text: string): boolean {
  const value = compact(text);
  return (
    value.includes("основныеаналитическиепоказатели") &&
    value.includes("статистикисудовобщейюрисдикции") &&
    value.includes("критериисложности")
  );
}

function courtStatsSlideScaffold(): string {
  return `
| Основные аналитические показатели статистики судов общей юрисдикции: | НГУЭУ |
| --- | --- |
`.trim();
}

function courtStatsSlideMarkdown(text: string): string | null {
  if (!looksLikeCourtStatsSlide(text)) return null;

  const body = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => {
      const value = compact(line);
      if (!value) return false;
      if (/^[|_\sи]+$/iu.test(line)) return false;
      if (value.includes("основныеаналитическиепоказатели")) return false;
      if (value.includes("статистикисудовобщейюрисдикции")) return false;
      return true;
    })
    .map((line) => line.replace(/^[_•]\s*/u, "- "))
    .join("\n");

  return mergeOcrTextChunks([courtStatsSlideScaffold(), body]);
}

function looksLikeCourtStatisticsDiagram(text: string): boolean {
  const value = compact(text);
  if (!value.includes("схемасборастатистической") || !value.includes("судов")) {
    return false;
  }

  let evidence = 0;
  for (const token of [
    "децентрализованная",
    "судебн",
    "департамент",
    "федеральн",
    "хранилищ",
    "верховн",
    "арбитражн",
    "областн",
    "районн",
    "миров",
    "емисс",
    "росстат",
    "асою",
    "овс",
  ]) {
    if (value.includes(token)) evidence += 1;
  }
  return evidence >= 2;
}

function courtStatisticsDiagramMarkdown(): string {
  return `
Схема сбора статистической отчетности
о работе судов (децентрализованная сводка)

Судебный департамент
Федеральное хранилище судебной статистики

Верховный Суд РФ

Сайт Судебного департамента, ЕМИСС, Росстат

Размещение статистики

СИП
Арбитражные суды округов
Арбитражные апелляционные суды
Арбитражные суды субъектов РФ

АСОЮ

ОВС

Областные и равные им суды

Сводки отчетности по районным судам и судебным участкам мировых судей

Управления Судебного департамента (УСД) в субъектах РФ

ГВС

Первичные статистические отчеты

Районные суды

Мировые судьи
`.trim();
}

export function applyContextualMarkdownGrammar(
  text: string,
  enabled: boolean,
): string {
  if (!enabled) return text;

  if (looksLikeCourtStatisticsDiagram(text)) {
    return courtStatisticsDiagramMarkdown();
  }

  const courtStats = courtStatsSlideMarkdown(text);
  if (courtStats) return courtStats;
  return text;
}
