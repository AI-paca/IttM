/**
 * ============================================================================
 *  ТИПЫ 3D-СТИКЕРА (StickerBg)
 * ============================================================================
 * Вынесено из StickerBg.tsx для уменьшения связности и повторного
 * использования геометрических утилит в других компонентах.
 * ============================================================================
 */

export type Corner = "tr" | "tl" | "br" | "bl";
export type Shape = "circle" | "rect";
export type AnimMode = "peel" | "roll" | "none";

export interface Point {
  A: [number, number];
  B: [number, number];
  C: [number, number];
}

export interface Crease {
  c1: [number, number];
  c2: [number, number];
  path: string;
}

export interface RollStrip {
  path: string;
  transform: string;
  opacity: number;
  shade: number;
}

export interface StickerBgProps {
  peeled: boolean;
  active: boolean;
  className?: string;
  children?: React.ReactNode;
  initialSize?: { w: number; h: number };
  oversizePct?: number;
  corner?: Corner;
  baseDx?: number;
  baseDy?: number;
  curve?: number;
  shadow?: number;
  r?: number;
  animMode?: AnimMode;
}

/** Направление для каждого угла: [signX, signY] */
export const CORNER_DIR: Record<Corner, [number, number]> = {
  tr: [1, -1],
  tl: [-1, -1],
  br: [1, 1],
  bl: [-1, 1],
};
