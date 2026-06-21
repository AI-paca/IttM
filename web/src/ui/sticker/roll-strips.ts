/**
 * ============================================================================
 *  РАСЧЁТ ROLL-ПОЛОС ДЛЯ 3D-СТИКЕРА
 * ============================================================================
 * Чистая функция расчёта полос для режима анимации "roll".
 * Разделена из StickerBg.tsx для тестируемости и переиспользования.
 * ============================================================================
 */

import { clamp } from "./geometry";
import { CORNER_DIR } from "./types";
import type { Corner, RollStrip } from "./types";

const STRIPS = 18;

export function buildRollStrips(
  w: number,
  h: number,
  corner: Corner,
  anim: number,
): RollStrip[] {
  if (anim <= 0) return [];

  const scx = w / 2;
  const scy = h / 2;
  const dir = CORNER_DIR[corner];
  const axisAngle = Math.atan2(dir[1], dir[0]) + Math.PI / 2;
  const axisX = Math.cos(axisAngle);
  const axisY = Math.sin(axisAngle);
  const perpX = -dir[1];
  const perpY = dir[0];

  const strips: RollStrip[] = [];

  for (let i = 0; i < STRIPS; i++) {
    const t0 = i / STRIPS;
    const t1 = (i + 1) / STRIPS;
    const tMid = (t0 + t1) / 2;

    const localT = clamp((anim - (1 - tMid)) / 0.5, 0, 1);
    if (localT <= 0) continue;

    const rollAngle = localT * Math.PI;
    const cosA = Math.cos(rollAngle);
    const shade = cosA > 0 ? 1 - cosA * 0.5 : 0;
    const scaleAlongPerp = Math.abs(cosA);
    const isBack = cosA < 0;

    const px0 =
      scx + perpX * (t0 - 0.5) * (Math.abs(perpX) * w + Math.abs(perpY) * h);
    const py0 =
      scy + perpY * (t0 - 0.5) * (Math.abs(perpX) * w + Math.abs(perpY) * h);
    const px1 =
      scx + perpX * (t1 - 0.5) * (Math.abs(perpX) * w + Math.abs(perpY) * h);
    const py1 =
      scy + perpY * (t1 - 0.5) * (Math.abs(perpX) * w + Math.abs(perpY) * h);

    const hw = (Math.abs(axisX) * w + Math.abs(axisY) * h) / 2;
    const corners: [number, number][] = [
      [px0 - axisX * hw, py0 - axisY * hw],
      [px0 + axisX * hw, py0 + axisY * hw],
      [px1 + axisX * hw, py1 + axisY * hw],
      [px1 - axisX * hw, py1 - axisY * hw],
    ];
    const stripPath = `M ${corners[0][0]} ${corners[0][1]} L ${corners[1][0]} ${corners[1][1]} L ${corners[2][0]} ${corners[2][1]} L ${corners[3][0]} ${corners[3][1]} Z`;

    const midX = (px0 + px1) / 2;
    const midY = (py0 + py1) / 2;
    const transform = `translate(${midX} ${midY}) scale(${
      Math.abs(perpX) < 0.5 ? 1 : scaleAlongPerp
    } ${
      Math.abs(perpY) < 0.5 ? 1 : scaleAlongPerp
    }) translate(${-midX} ${-midY})`;

    strips.push({
      path: stripPath,
      transform,
      opacity: 1,
      shade: isBack ? 0.7 : shade,
    });
  }

  return strips;
}
