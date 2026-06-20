/**
 * ============================================================================
 *  БЕЙДЖИ БЕЗОПАСНОСТИ ИСТОЧНИКОВ OCR
 * ============================================================================
 * Рефакторинг: вместо хардкодных Tailwind-классов каждый бейдж ссылается на
 * семантический токен из ui/theme/tokens.css (success/danger/warning/info).
 * Менять палитру можно централизованно в tokens.css.
 * ============================================================================
 */

export type BadgeTone = "success" | "danger" | "warning" | "info" | "muted";

export interface SourceBadge {
  /** Семантический тон бейджа (маппится на CSS-классы .badge-*). */
  tone: BadgeTone;
  /** CSS-переменная фона "тёмного" слоя (под отклеенным стикером). */
  bgVar: string;
  text: string;
  desc: string;
  /** Нейтральный бейдж — текст тёмный даже на цветном фоне. */
  isNeutral?: boolean;
}

/** Декларативная таблица: id источника → бейдж. Редактируется в одном месте. */
const BADGES: Record<string, SourceBadge> = {
  browser: {
    tone: "success",
    bgVar: "var(--color-success)",
    text: "Безопасно.",
    desc: "API относительно безопасны. Локальная обработка. Данные не покидают устройство.",
  },
  auto: {
    tone: "info",
    bgVar: "var(--color-info-soft)",
    text: "Автоматически",
    desc: "Безопасность зависит от выбранного моделью метода (может использовать сеть)\nCloud -> Local -> Browser",
    isNeutral: true,
  },
  local_tess: {
    tone: "warning",
    bgVar: "var(--color-warning)",
    text: "Слабо безопасно.",
    desc: "Отправка на сервер предприятия. Безопасность зависит от ваших админов.",
  },
  local_easy: {
    tone: "warning",
    bgVar: "var(--color-warning)",
    text: "Слабо безопасно.",
    desc: "Отправка на сервер предприятия. Безопасность зависит от ваших админов.",
  },
  gateway: {
    tone: "warning",
    bgVar: "var(--color-warning)",
    text: "Слабо безопасно.",
    desc: "Отправка на сервер предприятия. Безопасность зависит от ваших админов.",
  },
  llm: {
    tone: "danger",
    bgVar: "var(--color-danger)",
    text: "Опасность.",
    desc: "Данные отправляются Корпорациям (поставщикам AI) для обработки.",
  },
};

/**
 * Возвращает бейдж безопасности для источника OCR.
 * @returns null, если источник неизвестен.
 */
export function getSourceBadge(id: string): SourceBadge | null {
  return BADGES[id] ?? null;
}
