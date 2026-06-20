/**
 * ============================================================================
 *  ХУК АНИМАЦИИ И РАЗМЕРА 3D-СТИКЕРА
 * ============================================================================
 * Инкапсулирует ResizeObserver (отслеживание размеров контейнера) и
 * requestAnimationFrame-анимацию (peel/roll). Возвращает текущее
 * прогресс-значение анимации [0..1] и размеры.
 *
 * Защита от короткого blink при переполнении/переразметке сайдбара:
 *  1. Первое измерение делается в layout phase до paint.
 *  2. Нулевые/схлопнутые размеры игнорируются после первого валидного замера.
 *  3. Размер округляется до пикселя, чтобы subpixel-jitter не пересобирал SVG.
 * ============================================================================
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { easeInOut } from "./geometry";

export interface StickerSize {
  w: number;
  h: number;
}

const ANIM_DURATION = 450;
const MIN_VALID_SIZE = 1;
const EMPTY_SIZE: StickerSize = { w: 0, h: 0 };

function normalizeSize({ w, h }: StickerSize): StickerSize | null {
  const next = {
    w: Math.round(w),
    h: Math.round(h),
  };
  return next.w >= MIN_VALID_SIZE && next.h >= MIN_VALID_SIZE ? next : null;
}

function sameSize(a: StickerSize | null, b: StickerSize): boolean {
  return Boolean(a && a.w === b.w && a.h === b.h);
}

function sizeFromEntry(entry: ResizeObserverEntry): StickerSize {
  const borderBox = Array.isArray(entry.borderBoxSize)
    ? entry.borderBoxSize[0]
    : entry.borderBoxSize;
  if (borderBox) {
    return { w: borderBox.inlineSize, h: borderBox.blockSize };
  }
  return { w: entry.contentRect.width, h: entry.contentRect.height };
}

export function useStickerAnimation(
  peeled: boolean,
  containerRef: React.RefObject<HTMLDivElement | null>,
  initialSize?: StickerSize,
) {
  const initialValidSizeRef = useRef(normalizeSize(initialSize ?? EMPTY_SIZE));
  const [size, setSize] = useState<StickerSize>(
    initialValidSizeRef.current ?? EMPTY_SIZE,
  );
  const [anim, setAnim] = useState(0);
  const animRef = useRef(0);
  const rafRef = useRef(0);
  const lastValidSizeRef = useRef<StickerSize | null>(
    initialValidSizeRef.current,
  );

  // Отслеживание размеров контейнера через ResizeObserver. Не интерполируем
  // размер: SVG и HTML clip-path должны жить в одной системе координат.
  useLayoutEffect(() => {
    const node = containerRef.current;
    if (!node) return;

    const commitSize = (candidate: StickerSize) => {
      const next = normalizeSize(candidate);
      if (!next || sameSize(lastValidSizeRef.current, next)) return;
      lastValidSizeRef.current = next;
      setSize(next);
    };

    const rect = node.getBoundingClientRect();
    commitSize({ w: rect.width, h: rect.height });

    const ob = new ResizeObserver((entries) => {
      for (const entry of entries) {
        commitSize(sizeFromEntry(entry));
      }
    });
    ob.observe(node);

    return () => ob.disconnect();
  }, [containerRef]);

  // Анимация peel/roll через requestAnimationFrame
  useEffect(() => {
    const target = peeled ? 1 : 0;
    const startVal = animRef.current;
    const startTime = performance.now();
    cancelAnimationFrame(rafRef.current);
    const tick = (now: number) => {
      const t = Math.min(1, (now - startTime) / ANIM_DURATION);
      const val = startVal + (target - startVal) * easeInOut(t);
      animRef.current = val;
      setAnim(val);
      if (t < 1) rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafRef.current);
  }, [peeled]);

  return { size, anim };
}
