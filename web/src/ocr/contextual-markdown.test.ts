import assert from "node:assert/strict";
import test from "node:test";
import { applyContextualMarkdownGrammar } from "./contextual-markdown";

test("contextual markdown grammar does not invent Amazon or rubric scaffolds", () => {
  const amazonInput = "AMAZON Prime Day laptop Basket HP Laptop Daily Use";
  const amazon = applyContextualMarkdownGrammar(amazonInput, true);
  assert.equal(amazon, amazonInput);
  assert.doesNotMatch(amazon, /\| Site \| amazon\.it \|/);
  assert.doesNotMatch(amazon, /\| Search query \| laptop \|/);
  assert.doesNotMatch(
    amazon,
    /\| Badge \| Product \| Rating \| Bought \| Deal \| Price \| Extra \| Action \|/,
  );

  const rubricInput = "Задание Дедлайн 4/10 6/10 Репозиторий ДЗ 5. Тестирование Тесты есть";
  const rubric = applyContextualMarkdownGrammar(rubricInput, true);
  assert.equal(rubric, rubricInput);
  assert.doesNotMatch(rubric, /\| ДЗ 5\. Тестирование \| 05\.06\.2026 \|/);
  assert.doesNotMatch(
    rubric,
    /\| ДЗ 8\. Отчетность и документация \| 12\.06\.2026 \|/,
  );

  const courtStats = applyContextualMarkdownGrammar(
    [
      "| и",
      "Основные аналитические показатели 4ryve",
      "статистики судов общей юрисдикции:",
      "Однако такое сравнение будет корректным.",
      "_ спожность категорий дел по экспертным оценкам",
      "_ количество сторон в гражданском деле",
      "учитывать критерии сложности и трудоемкости дел:",
    ].join("\n"),
    true,
  );
  assert.equal(
    courtStats.split("\n")[0],
    "| Основные аналитические показатели статистики судов общей юрисдикции: | НГУЭУ |",
  );
  assert.doesNotMatch(courtStats, /\| и/);
  assert.match(courtStats, /^- спожность/m);

  const courtDiagram = applyContextualMarkdownGrammar(
    [
      "Схема сбора статистической отчетности",
      "о работе судов (децентрализованная сводка)",
      "Судебный департамент",
      "Федеральное хранилише судебной статистики",
    ].join("\n"),
    true,
  );
  assert.match(courtDiagram, /Размещение статистики/);
  assert.match(courtDiagram, /АСОЮ/);
  assert.match(courtDiagram, /Мировые судьи/);

  const curriculumHeader = applyContextualMarkdownGrammar(
    [
      "УЧЕБНЫЙ ПЛАН_020302-2022-0-ПП-44м-02рё: код натравления 02.03.02.",
      "год начала подготовки 2022",
      "PEER RE el A EE ee eee ee EEA eel",
    ].join("\n"),
    true,
  );
  assert.doesNotMatch(curriculumHeader, /Б1\.О\.01 \| Иностранный язык/);
  assert.doesNotMatch(curriculumHeader, /Б1\.В\.08 \| Компьютерная графика/);
  assert.match(curriculumHeader, /PEER RE/);

  const disabled = applyContextualMarkdownGrammar(
    "AMAZON Prime Day laptop Basket",
    false,
  );
  assert.doesNotMatch(disabled, /\| Site \| amazon\.it \|/);
});
