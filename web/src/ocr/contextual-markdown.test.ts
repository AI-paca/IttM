import assert from "node:assert/strict";
import test from "node:test";
import { applyContextualMarkdownGrammar } from "./contextual-markdown";

test("contextual markdown grammar does not replace OCR with canned scaffolds", () => {
  const samples = [
    "AMAZON Prime Day laptop Basket HP Laptop Daily Use",
    "Задание Дедлайн 4/10 6/10 Репозиторий ДЗ 5. Тестирование Тесты есть",
    [
      "| и",
      "Основные аналитические показатели 4ryve",
      "статистики судов общей юрисдикции:",
      "Однако такое сравнение будет корректным.",
      "_ спожность категорий дел по экспертным оценкам",
    ].join("\n"),
    [
      "Схема сбора статистической отчетности",
      "о работе судов (децентрализованная сводка)",
      "Судебный департамент",
      "Федеральное хранилише судебной статистики",
    ].join("\n"),
    [
      "УЧЕБНЫЙ ПЛАН_020302-2022-0-ПП-44м-02рё: код натравления 02.03.02.",
      "год начала подготовки 2022",
      "PEER RE el A EE ee eee ee EEA eel",
    ].join("\n"),
  ];

  for (const sample of samples) {
    assert.equal(applyContextualMarkdownGrammar(sample, true), sample);
    assert.equal(applyContextualMarkdownGrammar(sample, false), sample);
  }
});
