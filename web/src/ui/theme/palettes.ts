/**
 * ============================================================================
 *  ПАЛИТРЫ ТЕМЫ + ЦВЕТОВАЯ МАТЕМАТИКА (ЭРГОНОМИЧНЫЙ ЭТАЛОН)
 * ============================================================================
 *
 * Источник истины для базовой темы интерфейса.
 * WORKING_THEMES описывает только среду: фон, surfaces, borders, текст и общий
 * accent. Цвета стикеров источников задаются через семантическую шкалу
 * безопасности в ../source-security и дальше выводятся под текущую тему.
 * ============================================================================
 */

import {
  SOURCE_SAFETY_BY_SOURCE,
  SOURCE_SAFETY_TIER_SEEDS,
} from "../source-security";

export interface ThemePalette {
  base_background: string;
  dropzone_normal: string;
  dropzone_active: string;
  border: string;
  text_primary: string;
  text_secondary: string;
  accent_feedback: string;
}

/** Крайнее тёмное положение (ручной выбор / OLED Pro Black). */
export const PURE_DARK: ThemePalette = {
  base_background: "#07090B",
  dropzone_normal: "#101215",
  dropzone_active: "#1A1D22",
  border: "#20242A",
  text_primary: "#E7EAED",
  text_secondary: "#9AA2AB",
  accent_feedback: "#58A6FF",
};

/** VS Code modern dark: почти чёрный рабочий chrome с холодным cyan-акцентом. */
export const VSCODE_DARK_MODERN: ThemePalette = {
  base_background: "#121314",
  dropzone_normal: "#191A1B",
  dropzone_active: "#242526",
  border: "#2A2B2C",
  text_primary: "#BBBEBF",
  text_secondary: "#8C8C8C",
  accent_feedback: "#3994BC",
};

/** VS Code Dark+ / Visual Studio: классический графит и синий selection. */
export const VSCODE_DARK_PLUS: ThemePalette = {
  base_background: "#1E1E1E",
  dropzone_normal: "#252526",
  dropzone_active: "#2D2D30",
  border: "#3C3C3C",
  text_primary: "#D4D4D4",
  text_secondary: "#A6A6A6",
  accent_feedback: "#007ACC",
};

/** GitHub Dark Dimmed: мягкий сине-серый тёмный workbench. */
export const GITHUB_DARK_DIMMED: ThemePalette = {
  base_background: "#24292F",
  dropzone_normal: "#2C333A",
  dropzone_active: "#37404A",
  border: "#444C56",
  text_primary: "#D7DEE6",
  text_secondary: "#A9B4C0",
  accent_feedback: "#6EA6F8",
};

/** JetBrains Darcula: классический нейтральный dark для долгой работы. */
export const JETBRAINS_DARCULA: ThemePalette = {
  base_background: "#2B2B2B",
  dropzone_normal: "#333333",
  dropzone_active: "#3D3D3D",
  border: "#4A4A4A",
  text_primary: "#DADADA",
  text_secondary: "#A9A9A9",
  accent_feedback: "#6897BB",
};

/** One Dark Pro: популярный slate-blue участок без неона. */
export const ONE_DARK_PRO: ThemePalette = {
  base_background: "#30343F",
  dropzone_normal: "#383D49",
  dropzone_active: "#444A58",
  border: "#515969",
  text_primary: "#E4E7ED",
  text_secondary: "#ABB2BF",
  accent_feedback: "#61AFEF",
};

/** Monokai / terminal graphite: единственный olive/green checkpoint шкалы. */
export const MONOKAI_GRAPHITE: ThemePalette = {
  base_background: "#3A3A32",
  dropzone_normal: "#44443A",
  dropzone_active: "#515145",
  border: "#5F5F50",
  text_primary: "#F4F1E4",
  text_secondary: "#C3BFA4",
  accent_feedback: "#A6E22E",
};

/** Dimmed Workbench Gray: мост из dark IDE в light без резкого скачка. */
export const DIMMED_WORKBENCH_GRAY: ThemePalette = {
  base_background: "#555A62",
  dropzone_normal: "#60666F",
  dropzone_active: "#6C737D",
  border: "#78818C",
  text_primary: "#F0F2F4",
  text_secondary: "#C7CDD4",
  accent_feedback: "#8FA1B3",
};

/** Classic Workbench Gray: средне-светлый, спокойный рабочий checkpoint. */
export const CLASSIC_WORKBENCH_GRAY: ThemePalette = {
  base_background: "#7C838C",
  dropzone_normal: "#8B929B",
  dropzone_active: "#9AA1AA",
  border: "#A7ADB5",
  text_primary: "#151A20",
  text_secondary: "#323A43",
  accent_feedback: "#6F7F8F",
};

/** Soft Light Workbench: промежуточная light IDE до почти белого режима. */
export const SOFT_LIGHT_WORKBENCH: ThemePalette = {
  base_background: "#B8BEC7",
  dropzone_normal: "#AEB5BF",
  dropzone_active: "#9FA8B4",
  border: "#9BA3AE",
  text_primary: "#202326",
  text_secondary: "#4B5563",
  accent_feedback: "#4C6F93",
};

/** VS Code Light / JetBrains Light family: спокойная светлая IDE. */
export const VSCODE_LIGHT: ThemePalette = {
  base_background: "#E6E8EC",
  dropzone_normal: "#D9DDE3",
  dropzone_active: "#C7CED8",
  border: "#C6CCD4",
  text_primary: "#202326",
  text_secondary: "#5A626D",
  accent_feedback: "#3B6EA8",
};

/** Крайнее светлое положение (ручной выбор / Pure White). */
export const PURE_LIGHT: ThemePalette = {
  base_background: "#F4F6F8",
  dropzone_normal: "#E6EAEE",
  dropzone_active: "#CBD6E0",
  border: "#D3D9DF",
  text_primary: "#202326",
  text_secondary: "#5F666E",
  accent_feedback: "#2F5F8F",
};

/** Авто-тёмная (следует за браузером). */
export const AUTO_DARK: ThemePalette = { ...VSCODE_DARK_MODERN };
/** Авто-светлая (следует за браузером). */
export const AUTO_LIGHT: ThemePalette = { ...VSCODE_LIGHT };

/**
 * ПОЛНАЯ ШКАЛА РАБОЧИХ ТЕМ (для интерполяции ползунком).
 *
 * Чекпоинты распределены по светлоте без резкого прыжка из dark в light.
 * Цветная часть остаётся приглушённой: это рабочая IDE-шкала, а не rainbow.
 */
export const WORKING_THEMES: ThemePalette[] = [
  PURE_DARK,
  VSCODE_DARK_MODERN,
  VSCODE_DARK_PLUS,
  GITHUB_DARK_DIMMED,
  JETBRAINS_DARCULA,
  ONE_DARK_PRO,
  MONOKAI_GRAPHITE,
  DIMMED_WORKBENCH_GRAY,
  CLASSIC_WORKBENCH_GRAY,
  SOFT_LIGHT_WORKBENCH,
  VSCODE_LIGHT,
  PURE_LIGHT,
];

/* ----------------------------------------------------------------- helpers */

type RGB = [number, number, number];

function parseHex(hex: string): RGB {
  const h = hex.replace("#", "");
  const full =
    h.length === 3
      ? h
          .split("")
          .map((c) => c + c)
          .join("")
      : h;
  const n = parseInt(full, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function toHex([r, g, b]: RGB): string {
  const c = (v: number) =>
    Math.max(0, Math.min(255, Math.round(v)))
      .toString(16)
      .padStart(2, "0");
  return `#${c(r)}${c(g)}${c(b)}`;
}

function mix(a: string, b: string, t: number): string {
  const ca = parseHex(a);
  const cb = parseHex(b);
  return toHex([
    ca[0] + (cb[0] - ca[0]) * t,
    ca[1] + (cb[1] - ca[1]) * t,
    ca[2] + (cb[2] - ca[2]) * t,
  ]);
}

function lighten(hex: string, t: number): string {
  return mix(hex, "#ffffff", t);
}

function darken(hex: string, t: number): string {
  return mix(hex, "#000000", t);
}

function hexToHsl(hex: string): [number, number, number] {
  const [r, g, b] = parseHex(hex).map((v) => v / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const l = (max + min) / 2;
  let h = 0;
  let s = 0;
  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    switch (max) {
      case r:
        h = ((g - b) / d + (g < b ? 6 : 0)) * 60;
        break;
      case g:
        h = ((b - r) / d + 2) * 60;
        break;
      default:
        h = ((r - g) / d + 4) * 60;
    }
  }
  return [h, s * 100, l * 100];
}

function hslToHex(h: number, s: number, l: number): string {
  const hh = ((h % 360) + 360) % 360;
  const ss = Math.max(0, Math.min(100, s)) / 100;
  const ll = Math.max(0, Math.min(100, l)) / 100;
  const c = (1 - Math.abs(2 * ll - 1)) * ss;
  const x = c * (1 - Math.abs(((hh / 60) % 2) - 1));
  const m = ll - c / 2;
  let r: number;
  let g: number;
  let b: number;
  if (hh < 60) [r, g, b] = [c, x, 0];
  else if (hh < 120) [r, g, b] = [x, c, 0];
  else if (hh < 180) [r, g, b] = [0, c, x];
  else if (hh < 240) [r, g, b] = [0, x, c];
  else if (hh < 300) [r, g, b] = [x, 0, c];
  else [r, g, b] = [c, 0, x];
  return toHex([(r + m) * 255, (g + m) * 255, (b + m) * 255]);
}

function rgba(hex: string, alpha: number): string {
  const [r, g, b] = parseHex(hex);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function luminance(hex: string): number {
  const [r, g, b] = parseHex(hex).map((v) => v / 255);
  const lin = (c: number) =>
    c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

/* --------------------------------------------------------- интерполяция */

function smoothstep(t: number): number {
  return t * t * (3 - 2 * t);
}

const clampL = (v: number) => Math.max(0, Math.min(100, v));
const clampS = (v: number) => Math.max(0, Math.min(100, v));

function textLightness(bgLit: number, isPrimary: boolean): number {
  if (bgLit < 50) return isPrimary ? 92 : 68;
  return isPrimary ? 14 : 40;
}

/**
 * Интерполяция между checkpoint-палитрами.
 *
 * Основной фон смешивается напрямую в RGB: для рабочих IDE-тем это спокойнее,
 * чем HSL-дуга, которая между приглушёнными серыми может внезапно проходить
 * через заметный красный/зелёный оттенок. Остальные surface/text токены
 * выводятся из уже смешанного фона.
 */
export function interpolatePalette(
  a: ThemePalette,
  b: ThemePalette,
  t: number,
): ThemePalette {
  const k = Math.max(0, Math.min(1, t));
  if (k <= 0) return { ...a };
  if (k >= 1) return { ...b };

  const sk = smoothstep(k);

  const base = mix(a.base_background, b.base_background, sk);
  const [hue, sat, lit] = hexToHsl(base);

  const isDark = lit < 50;

  const dropNorm = hslToHex(hue, sat, clampL(isDark ? lit + 4 : lit - 4));
  const dropAct = hslToHex(
    hue,
    clampS(sat + 6),
    clampL(isDark ? lit + 8 : lit - 9),
  );
  const border = hslToHex(hue, sat, clampL(isDark ? lit + 8 : lit - 10));

  const textPrimary = hslToHex(hue, sat * 0.5, textLightness(lit, true));
  const textSecondary = hslToHex(hue, sat * 0.5, textLightness(lit, false));
  const accent = mix(a.accent_feedback, b.accent_feedback, sk);

  return {
    base_background: base,
    dropzone_normal: dropNorm,
    dropzone_active: dropAct,
    border,
    text_primary: textPrimary,
    text_secondary: textSecondary,
    accent_feedback: accent,
  };
}

/**
 * ГЛАВНАЯ ФУНКЦИЯ ДЛЯ ПОЛЗУНКА:
 * Плавно скользит по всей шкале WORKING_THEMES (0..1) через чекпоинты.
 * Каждый сегмент интерполируется чистой HSL-кривой, поэтому путь проходит
 * только через валидные рабочие темы (без хроматического мусора).
 */
export function interpolateWorkingScale(t: number): ThemePalette {
  const k = Math.max(0, Math.min(1, t));
  const themes = WORKING_THEMES;
  const lastIdx = themes.length - 1;
  if (k <= 0) return { ...themes[0] };
  if (k >= 1) return { ...themes[lastIdx] };

  const scaled = k * lastIdx;
  const idx = Math.floor(scaled);
  const frac = scaled - idx;

  if (frac === 0) return { ...themes[idx] };
  return interpolatePalette(themes[idx], themes[idx + 1], frac);
}

/* --------------------------------------------- производство токенов */

export function isPaletteDark(p: ThemePalette): boolean {
  return luminance(p.base_background) < 0.5;
}

function contrastOn(hex: string): string {
  return luminance(hex) > 0.5 ? "#1A1206" : "#FFFFFF";
}

type StatusSet = {
  base: string;
  soft: string;
  border: string;
  text: string;
};

function deriveStatus(
  p: ThemePalette,
  baseColor: string,
  isDark: boolean,
): StatusSet {
  const bg = p.base_background;
  return {
    base: isDark ? lighten(baseColor, 0.1) : baseColor,
    soft: mix(bg, baseColor, isDark ? 0.18 : 0.12),
    border: mix(baseColor, bg, isDark ? 0.55 : 0.5),
    text: isDark ? lighten(baseColor, 0.18) : darken(baseColor, 0.12),
  };
}

type DerivedTone = StatusSet & {
  on: string;
};

type StickerFoldSet = {
  dark: string;
  mid: string;
  light: string;
  white: string;
  edge: string;
};

function deriveStickerFold(p: ThemePalette, isDark: boolean): StickerFoldSet {
  const bg = p.base_background;
  const accent = p.accent_feedback;
  const paper = mix(bg, accent, isDark ? 0.22 : 0.12);
  const [hue, sat, lit] = hexToHsl(paper);
  const foldSat = clampS(
    Math.max(isDark ? 12 : 8, Math.min(isDark ? 42 : 28, sat * 0.72)),
  );

  if (isDark) {
    return {
      dark: hslToHex(hue, foldSat, clampL(lit + 3)),
      mid: hslToHex(hue, foldSat, clampL(lit + 9)),
      light: hslToHex(hue, foldSat, clampL(lit + 16)),
      white: hslToHex(hue, foldSat * 0.82, clampL(lit + 25)),
      edge: hslToHex(hue, foldSat * 0.65, clampL(lit + 34)),
    };
  }

  return {
    dark: hslToHex(hue, foldSat, clampL(lit - 17)),
    mid: hslToHex(hue, foldSat, clampL(lit - 8)),
    light: hslToHex(hue, foldSat * 0.9, clampL(lit + 2)),
    white: hslToHex(hue, foldSat * 0.65, clampL(lit + 10)),
    edge: hslToHex(hue, foldSat * 0.45, clampL(lit + 16)),
  };
}

/**
 * Back-compat alias для UI-токенов: source id -> seed цвета его safety tier.
 * Если нужно поменять смысловой цвет, править SOURCE_SAFETY_TIER_SEEDS.
 */
export const SOURCE_STICKER_SEEDS = Object.fromEntries(
  Object.entries(SOURCE_SAFETY_BY_SOURCE).map(([sourceId, tier]) => [
    sourceId,
    SOURCE_SAFETY_TIER_SEEDS[tier],
  ]),
) as Record<keyof typeof SOURCE_SAFETY_BY_SOURCE, string>;

function deriveSourceTone(
  p: ThemePalette,
  seedColor: string,
  isDark: boolean,
): DerivedTone {
  const bg = p.base_background;
  const [seedHue, seedSat] = hexToHsl(seedColor);
  const [, bgSat, bgLit] = hexToHsl(bg);
  const themedSat = clampS(
    seedSat * (isDark ? 0.78 : 0.84) + bgSat * (isDark ? 0.14 : 0.1),
  );
  const themedLit = isDark
    ? clampL(46 + bgLit * 0.34)
    : clampL(40 + (bgLit - 70) * 0.18);
  const base = hslToHex(seedHue, themedSat, themedLit);
  return {
    base,
    soft: mix(bg, base, isDark ? 0.24 : 0.16),
    border: mix(base, p.border, isDark ? 0.42 : 0.52),
    text: hslToHex(
      seedHue,
      clampS(themedSat * (isDark ? 0.82 : 0.92)),
      clampL(themedLit + (isDark ? 18 : -15)),
    ),
    on: contrastOn(base),
  };
}

function sourceStickerTokens(
  p: ThemePalette,
  isDark: boolean,
): Record<string, string> {
  const result: Record<string, string> = {};

  for (const [sourceId, safetyTier] of Object.entries(
    SOURCE_SAFETY_BY_SOURCE,
  )) {
    const key = sourceId.replace(/_/g, "-");
    const seed = SOURCE_SAFETY_TIER_SEEDS[safetyTier];
    const tone = deriveSourceTone(p, seed, isDark);
    result[`source-${key}`] = tone.base;
    result[`source-${key}-soft`] = tone.soft;
    result[`source-${key}-border`] = tone.border;
    result[`source-${key}-text`] = tone.text;
    result[`source-${key}-on`] = tone.on;
  }

  return result;
}

const STATUS_LIGHT = {
  success: "#16a34a",
  danger: "#dc2626",
  warning: "#d97706",
  info: "#6366f1",
};

const STATUS_DARK = {
  success: "#4ade80",
  danger: "#f87171",
  warning: "#fbbf24",
  info: "#818cf8",
};

export function deriveTokens(p: ThemePalette): Record<string, string> {
  const dark = isPaletteDark(p);
  const bg = p.base_background;
  const accent = p.accent_feedback;
  const border = p.border;
  const statusSrc = dark ? STATUS_DARK : STATUS_LIGHT;

  const succ = deriveStatus(p, statusSrc.success, dark);
  const dang = deriveStatus(p, statusSrc.danger, dark);
  const warn = deriveStatus(p, statusSrc.warning, dark);
  const info = deriveStatus(p, statusSrc.info, dark);
  const stickerFold = deriveStickerFold(p, dark);

  return {
    accent,
    "accent-hover": dark ? darken(accent, 0.08) : darken(accent, 0.1),
    "accent-soft": mix(bg, accent, dark ? 0.16 : 0.12),
    "accent-soft-border": mix(accent, border, 0.45),
    "accent-strong": dark ? lighten(accent, 0.12) : darken(accent, 0.18),
    "accent-ring": rgba(accent, 0.5),

    "bg-app": bg,
    "bg-surface": p.dropzone_normal,
    "bg-elevated": p.dropzone_active,
    "bg-inset": dark ? lighten(bg, 0.03) : darken(bg, 0.03),
    "bg-overlay": dark ? "rgba(0, 0, 0, 0.5)" : "rgba(17, 24, 39, 0.4)",

    "text-primary": p.text_primary,
    "text-secondary": p.text_secondary,
    "text-muted": mix(p.text_secondary, bg, 0.45),
    "text-faint": mix(p.text_secondary, bg, 0.7),
    "text-on-accent": contrastOn(accent),
    "text-link": accent,

    "border-default": border,
    "border-strong": dark ? lighten(border, 0.05) : darken(border, 0.06),
    "border-subtle": dark ? darken(border, 0.02) : lighten(border, 0.03),
    "border-accent": mix(accent, border, 0.5),

    "shadow-accent": `0 20px 60px -15px ${rgba(accent, 0.3)}`,
    "shadow-accent-glow": `0 0 30px ${rgba(accent, 0.6)}`,

    success: succ.base,
    "success-soft": succ.soft,
    "success-border": succ.border,
    "success-text": succ.text,
    danger: dang.base,
    "danger-soft": dang.soft,
    "danger-border": dang.border,
    "danger-text": dang.text,
    warning: warn.base,
    "warning-soft": warn.soft,
    "warning-border": warn.border,
    "warning-text": warn.text,
    info: info.base,
    "info-soft": info.soft,
    "info-border": info.border,
    "info-text": info.text,

    "sticker-fold-dark": stickerFold.dark,
    "sticker-fold-mid": stickerFold.mid,
    "sticker-fold-light": stickerFold.light,
    "sticker-fold-white": stickerFold.white,
    "sticker-edge": stickerFold.edge,

    ...sourceStickerTokens(p, dark),
  };
}

const PREFIX = "--color-";

export function applyThemeVars(tokens: Record<string, string>): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  for (const [key, value] of Object.entries(tokens)) {
    root.style.setProperty(PREFIX + key, value);
  }
}

function syncBrowserThemeColor(p: ThemePalette): void {
  const root = document.documentElement;
  const dark = isPaletteDark(p);
  root.style.colorScheme = dark ? "dark" : "light";

  let themeMeta = document.querySelector<HTMLMetaElement>(
    'meta[name="theme-color"]',
  );
  if (!themeMeta) {
    themeMeta = document.createElement("meta");
    themeMeta.name = "theme-color";
    document.head.appendChild(themeMeta);
  }
  themeMeta.content = p.base_background;
}

export function applyPalette(p: ThemePalette): void {
  if (typeof document === "undefined") return;
  applyThemeVars(deriveTokens(p));
  document.documentElement.classList.toggle("dark", isPaletteDark(p));
  syncBrowserThemeColor(p);
}
