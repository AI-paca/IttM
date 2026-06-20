/**
 * ============================================================================
 *  БЕЙДЖИ БЕЗОПАСНОСТИ ИСТОЧНИКОВ OCR
 * ============================================================================
 * Текст бейджа живёт здесь, а цвет берётся из safety tier источника.
 * Сами уровни безопасности и их seed-цвета задаются в ui/source-security.ts.
 * ============================================================================
 */

import type { SourceType } from "../../ocr/types";
import { getSourceSafetyTier, type SourceSafetyTier } from "../source-security";

export type BadgeTone = SourceSafetyTier;

export interface SourceBadge {
  /** Семантический тон бейджа (для смысла, не для подбора цвета). */
  tone: BadgeTone;
  /** CSS-переменная фона "тёмного" слоя (под отклеенным стикером). */
  bgVar: string;
  textVar: string;
  descVar: string;
  text: string;
  desc: string;
}

type SourceBadgeCopy = Omit<SourceBadge, "tone">;

/** Декларативная таблица: id источника → текст бейджа. */
const BADGES: Record<SourceType, SourceBadgeCopy> = {
  browser: {
    bgVar: "var(--color-source-browser)",
    textVar: "var(--color-source-browser-on)",
    descVar: "var(--color-source-browser-on)",
    text: "Безопасно.",
    desc: "API относительно безопасны. Локальная обработка. Данные не покидают устройство.",
  },
  auto: {
    bgVar: "var(--color-source-auto-soft)",
    textVar: "var(--color-source-auto-text)",
    descVar: "var(--color-source-auto-text)",
    text: "Переменная безопасность.",
    desc: "Безопасность зависит от выбранного моделью метода (может использовать сеть)\nCloud -> Local -> Browser",
  },
  local_tess: {
    bgVar: "var(--color-source-local-tess)",
    textVar: "var(--color-source-local-tess-on)",
    descVar: "var(--color-source-local-tess-on)",
    text: "Контролируемый сервер.",
    desc: "Отправка на сервер предприятия. Безопасность зависит от ваших админов.",
  },
  local_easy: {
    bgVar: "var(--color-source-local-easy)",
    textVar: "var(--color-source-local-easy-on)",
    descVar: "var(--color-source-local-easy-on)",
    text: "Контролируемый сервер.",
    desc: "Отправка на сервер предприятия. Безопасность зависит от ваших админов.",
  },
  gateway: {
    bgVar: "var(--color-source-gateway)",
    textVar: "var(--color-source-gateway-on)",
    descVar: "var(--color-source-gateway-on)",
    text: "Контролируемый сервер.",
    desc: "Отправка на сервер предприятия. Безопасность зависит от ваших админов.",
  },
  llm: {
    bgVar: "var(--color-source-llm)",
    textVar: "var(--color-source-llm-on)",
    descVar: "var(--color-source-llm-on)",
    text: "Опасность.",
    desc: "Данные отправляются Корпорациям (поставщикам AI) для обработки.",
  },
};

/**
 * Возвращает бейдж безопасности для источника OCR.
 * @returns null, если источник неизвестен.
 */
export function getSourceBadge(id: string): SourceBadge | null {
  const sourceId = id as SourceType;
  const badge = BADGES[sourceId];
  const tone = getSourceSafetyTier(sourceId);
  if (!badge || !tone) return null;
  return { tone, ...badge };
}
