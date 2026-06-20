/**
 * ============================================================================
 *  ПАЛИТРЫ ТЕМЫ + ЦВЕТОВАЯ МАТЕМАТИКА (ЭРГОНОМИЧНЫЙ ЭТАЛОН)
 * ============================================================================
 *
 * Источник истины для цветов интерфейса.
 * Ползунок плавно скользит по шкале профессиональных рабочих тем WORKING_THEMES
 * (Black -> Pro RGB Hues -> Light Gray -> Pure White) без хроматических скачков.
 * ============================================================================
 */

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
  base_background: "#0B0D0E",
  dropzone_normal: "#151719",
  dropzone_active: "#212B36",
  border: "#1E2124",
  text_primary: "#E4E6E7",
  text_secondary: "#A1A6AA",
  accent_feedback: "#D6995C",
};

/** Профессиональный Сланцево-синий (Pro Slate). */
export const PRO_SLATE: ThemePalette = {
  base_background: "#141420",
  dropzone_normal: "#1E222E",
  dropzone_active: "#2B3245",
  border: "#282E3D",
  text_primary: "#E2E5EE",
  text_secondary: "#979EB2",
  accent_feedback: "#6085DE",
};

/** Профессиональный Шалфеевый (Pro Sage). */
export const PRO_SAGE: ThemePalette = {
  base_background: "#161C18",
  dropzone_normal: "#212923",
  dropzone_active: "#2E3B32",
  border: "#2C362F",
  text_primary: "#E3ECE5",
  text_secondary: "#98A69B",
  accent_feedback: "#5CA86B",
};

/** Профессиональный Теплый Мокко (Pro Warm Mocha). */
export const PRO_MOCHA: ThemePalette = {
  base_background: "#1E1915",
  dropzone_normal: "#2B241E",
  dropzone_active: "#3D332B",
  border: "#382F28",
  text_primary: "#EAE3DE",
  text_secondary: "#A69C94",
  accent_feedback: "#D6855C",
};

/** Авто-светлая / Мягкая бумага (Auto Light). */
export const AUTO_LIGHT: ThemePalette = {
  base_background: "#E9EBED",
  dropzone_normal: "#DBDEE1",
  dropzone_active: "#B9C4D0",
  border: "#C7CCD1",
  text_primary: "#242628",
  text_secondary: "#6D7378",
  accent_feedback: "#AD661F",
};

/** Крайнее светлое положение (ручной выбор / Pure White). */
export const PURE_LIGHT: ThemePalette = {
  base_background: "#F1F2F4",
  dropzone_normal: "#E3E6E8",
  dropzone_active: "#C2CCD6",
  border: "#CFD4D8",
  text_primary: "#242628",
  text_secondary: "#6D7378",
  accent_feedback: "#AD661F",
};

/** Авто-тёмная (следует за браузером). */
export const AUTO_DARK: ThemePalette = {
  base_background: "#1C1F26",
  dropzone_normal: "#252932",
  dropzone_active: "#2D3953",
  border: "#2E333E",
  text_primary: "#E4E5E7",
  text_secondary: "#9FA4AC",
  accent_feedback: "#D6AD5C",
};

/** ПОЛНАЯ ШКАЛА РАБОЧИХ ТЕМ (для интерполяции ползунком). */
export const WORKING_THEMES: ThemePalette[] = [
  PURE_DARK,
  PRO_SLATE,
  PRO_SAGE,
  PRO_MOCHA,
  AUTO_LIGHT,
  PURE_LIGHT,
];

/* ----------------------------------------------------------------- helpers */

type RGB = [number, number, number];

function parseHex(hex: string): RGB {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
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
  let r = 0,
    g = 0,
    b = 0;
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

function lerpAngle(a: number, b: number, t: number): number {
  const diff = (((b - a + 540) % 360) - 180) * t;
  return (a + diff + 360) % 360;
}

function textLightness(bgLit: number, isPrimary: boolean): number {
  if (bgLit < 50) return isPrimary ? 92 : 68;
  return isPrimary ? 14 : 40;
}

/**
 * Чистая HSL интерполяция между двумя любыми палитрами без лишних изгибов.
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

  const [aH, aS, aL] = hexToHsl(a.base_background);
  const [bH, bS, bL] = hexToHsl(b.base_background);

  const hue = lerpAngle(aH, bH, sk);
  const sat = clampS(aS + (bS - aS) * sk);
  const lit = clampL(aL + (bL - aL) * sk);
  const base = hslToHex(hue, sat, lit);

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

    "sticker-fold-dark": dark ? darken(bg, 0.02) : "#cbd5e1",
    "sticker-fold-mid": dark ? lighten(bg, 0.05) : "#e2e8f0",
    "sticker-fold-light": dark ? lighten(bg, 0.1) : "#f1f5f9",
    "sticker-fold-white": dark ? lighten(bg, 0.16) : "#ffffff",
    "sticker-edge": dark ? lighten(bg, 0.22) : "#ffffff",
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

export function applyPalette(p: ThemePalette): void {
  if (typeof document === "undefined") return;
  applyThemeVars(deriveTokens(p));
  document.documentElement.classList.toggle("dark", isPaletteDark(p));
}