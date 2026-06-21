export type AppState = "upload" | "configure" | "loading" | "reading";
/**
 * Положение ползунка темы в диапазоне 0..1:
 *   0 — крайняя тёмная (PURE_DARK), 1 — крайняя светлая (PURE_LIGHT).
 * В авто-режиме (`themeAuto=true`) уровень следует за системной темой браузера
 * и показывает положение в паре {AUTO_DARK, AUTO_LIGHT}.
 */
export type ThemeLevel = number;
export type NoticeTone = "error" | "success";

export interface Notice {
  message: string;
  tone: NoticeTone;
}
