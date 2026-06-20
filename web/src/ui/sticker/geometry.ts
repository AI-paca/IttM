/**
 * ============================================================================
 *  ЧИСТАЯ ГЕОМЕТРИЯ 3D-СТИКЕРА
 * ============================================================================
 * Функции без сайд-эффектов и без React — только расчёт SVG-путей и точек.
 * Легко тестировать отдельно и переиспользовать. Вынесено из StickerBg.tsx.
 * ============================================================================
 */

import type { Corner, Crease, Point } from "./types";

export function clamp(v: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, v));
}

export function easeInOut(t: number): number {
  return t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
}

export function getCornerPoints(
  x: number,
  y: number,
  w: number,
  h: number,
  corner: Corner,
  dx: number,
  dy: number,
): Point {
  switch (corner) {
    case "tr":
      return { A: [x + w - dx, y], B: [x + w, y + dy], C: [x + w, y] };
    case "tl":
      return { A: [x + dx, y], B: [x, y + dy], C: [x, y] };
    case "br":
      return {
        A: [x + w - dx, y + h],
        B: [x + w, y + h - dy],
        C: [x + w, y + h],
      };
    case "bl":
      return { A: [x + dx, y + h], B: [x, y + h - dy], C: [x, y + h] };
  }
}

export function foldInward(
  p: [number, number],
  a: [number, number],
  b: [number, number],
): [number, number] {
  const abx = b[0] - a[0],
    aby = b[1] - a[1];
  const apx = p[0] - a[0],
    apy = p[1] - a[1];
  const len2 = abx * abx + aby * aby;
  if (len2 === 0) return p;
  const t = (apx * abx + apy * aby) / len2;
  const proj: [number, number] = [a[0] + t * abx, a[1] + t * aby];
  return [proj[0] - (p[0] - proj[0]), proj[1] - (p[1] - proj[1])];
}

export function getCrease(
  A: [number, number],
  B: [number, number],
  C: [number, number],
  curve: number,
): Crease {
  const mx = (A[0] + B[0]) / 2,
    my = (A[1] + B[1]) / 2;
  const dxC = C[0] - mx,
    dyC = C[1] - my;
  const len = Math.hypot(dxC, dyC) || 1;
  const nx = dxC / len,
    ny = dyC / len;
  const off = curve * len;
  const c1: [number, number] = [
    A[0] + (B[0] - A[0]) * 0.33 + nx * off * 0.4,
    A[1] + (B[1] - A[1]) * 0.33 + ny * off * 0.4,
  ];
  const c2: [number, number] = [
    A[0] + (B[0] - A[0]) * 0.66 + nx * off * 0.4,
    A[1] + (B[1] - A[1]) * 0.66 + ny * off * 0.4,
  ];
  return {
    c1,
    c2,
    path: `M ${A[0]} ${A[1]} C ${c1[0]} ${c1[1]} ${c2[0]} ${c2[1]} ${B[0]} ${B[1]}`,
  };
}

export function buildFullPath(
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): string {
  const R = Math.min(r, w / 2, h / 2);
  return `M ${x + R} ${y} L ${x + w - R} ${y} Q ${x + w} ${y} ${x + w} ${y + R} L ${x + w} ${y + h - R} Q ${x + w} ${y + h} ${x + w - R} ${y + h} L ${x + R} ${y + h} Q ${x} ${y + h} ${x} ${y + h - R} L ${x} ${y + R} Q ${x} ${y} ${x + R} ${y} Z`;
}

export function buildMainPath(
  _x: number,
  _y: number,
  _w: number,
  _h: number,
  _r: number,
  A: [number, number],
  B: [number, number],
  corner: Corner,
): string {
  if (corner === "tr")
    return `M ${A[0]} ${A[1]} L ${B[0]} ${B[1]} L 10000 ${B[1]} L 10000 10000 L -10000 10000 L -10000 ${A[1]} Z`;
  if (corner === "tl")
    return `M ${A[0]} ${A[1]} L ${B[0]} ${B[1]} L -10000 ${B[1]} L -10000 10000 L 10000 10000 L 10000 ${A[1]} Z`;
  if (corner === "br")
    return `M ${A[0]} ${A[1]} L ${B[0]} ${B[1]} L 10000 ${B[1]} L 10000 -10000 L -10000 -10000 L -10000 ${A[1]} Z`;
  return `M ${A[0]} ${A[1]} L ${B[0]} ${B[1]} L -10000 ${B[1]} L -10000 -10000 L 10000 -10000 L 10000 ${A[1]} Z`;
}

export function buildFoldOuterEdge(
  A: [number, number],
  B: [number, number],
  Cprime: [number, number],
  curve: number,
): string {
  const bulge = curve * 1.4;
  const abx = B[0] - A[0],
    aby = B[1] - A[1];
  const len = Math.hypot(abx, aby) || 1;
  const nx = -aby / len,
    ny = abx / len;
  const mid: [number, number] = [(A[0] + B[0]) / 2, (A[1] + B[1]) / 2];
  const sign =
    (Cprime[0] - mid[0]) * nx + (Cprime[1] - mid[1]) * ny > 0 ? 1 : -1;
  const q1x = (A[0] + Cprime[0]) / 2 + sign * nx * bulge * 15;
  const q1y = (A[1] + Cprime[1]) / 2 + sign * ny * bulge * 15;
  const q2x = (Cprime[0] + B[0]) / 2 + sign * nx * bulge * 15;
  const q2y = (Cprime[1] + B[1]) / 2 + sign * ny * bulge * 15;
  return `M ${A[0]} ${A[1]} Q ${q1x} ${q1y} ${Cprime[0]} ${Cprime[1]} Q ${q2x} ${q2y} ${B[0]} ${B[1]}`;
}

export function buildFoldPath(
  A: [number, number],
  B: [number, number],
  C: [number, number],
  Cprime: [number, number],
  curve: number,
): string {
  const cr = getCrease(A, B, C, curve);
  const edge = buildFoldOuterEdge(A, B, Cprime, curve);
  return `${edge} C ${cr.c2[0]} ${cr.c2[1]} ${cr.c1[0]} ${cr.c1[1]} ${A[0]} ${A[1]} Z`;
}

export function buildFoldShadow(
  A: [number, number],
  B: [number, number],
  C: [number, number],
  curve: number,
): string {
  const cr = getCrease(A, B, C, curve);
  const off = -4;
  const dxC = C[0] - (A[0] + B[0]) / 2,
    dyC = C[1] - (A[1] + B[1]) / 2;
  const len = Math.hypot(dxC, dyC) || 1;
  const nx = dxC / len,
    ny = dyC / len;
  const A2: [number, number] = [A[0] + nx * off, A[1] + ny * off];
  const B2: [number, number] = [B[0] + nx * off, B[1] + ny * off];
  return `M ${A2[0]} ${A2[1]} C ${cr.c1[0] + nx * off} ${cr.c1[1] + ny * off} ${cr.c2[0] + nx * off} ${cr.c2[1] + ny * off} ${B2[0]} ${B2[1]} C ${cr.c2[0]} ${cr.c2[1]} ${cr.c1[0]} ${cr.c1[1]} ${A2[0]} ${A2[1]} Z`;
}

export function buildHighlight(
  A: [number, number],
  Cprime: [number, number],
  B: [number, number],
): string {
  const t = 0.15;
  const p1: [number, number] = [
    A[0] + (Cprime[0] - A[0]) * t,
    A[1] + (Cprime[1] - A[1]) * t,
  ];
  const p2: [number, number] = [
    B[0] + (Cprime[0] - B[0]) * t,
    B[1] + (Cprime[1] - B[1]) * t,
  ];
  return `M ${p1[0]} ${p1[1]} L ${p2[0]} ${p2[1]}`;
}

export interface StickerGeometry {
  fullPath: string;
  mainPath: string;
  foldPath: string;
  foldOuterEdgePath: string;
  shadowPath: string;
  highlightPath: string;
  crease: Crease;
}

export function buildStickerGeometry(
  w: number,
  h: number,
  corner: Corner,
  A: [number, number],
  B: [number, number],
  C: [number, number],
  Cprime: [number, number],
  curve: number,
  r: number,
): StickerGeometry {
  return {
    fullPath: buildFullPath(0, 0, w, h, r),
    mainPath: buildMainPath(0, 0, w, h, r, A, B, corner),
    foldPath: buildFoldPath(A, B, C, Cprime, curve),
    foldOuterEdgePath: buildFoldOuterEdge(A, B, Cprime, curve),
    shadowPath: buildFoldShadow(A, B, C, curve),
    highlightPath: buildHighlight(A, Cprime, B),
    crease: getCrease(A, B, C, curve),
  };
}
