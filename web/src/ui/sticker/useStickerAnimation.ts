/**
 * ============================================================================
 *  ХУК АНИМАЦИИ И РАЗМЕРА 3D-СТИКЕРА
 * ============================================================================
 * Инкапсулирует ResizeObserver (отслеживание размеров контейнера) и
 * requestAnimationFrame-анимацию (peel/roll). Возвращает текущее
 * прогресс-значение анимации [0..1] и размеры.
 * ============================================================================
 */

import { useEffect, useRef, useState } from "react";
import { easeInOut } from "./geometry";

const ANIM_DURATION = 450;

export interface StickerSize {
  w: number;
  h: number;
}

export function useStickerAnimation(
  peeled: boolean,
  containerRef: React.RefObject<HTMLDivElement | null>,
) {
  const [size, setSize] = useState<StickerSize>({ w: 0, h: 0 });
  const [anim, setAnim] = useState(0);
  const animRef = useRef(0);
  const rafRef = useRef(0);

  // Отслеживание размеров контейнера через ResizeObserver
  useEffect(() => {
    if (!containerRef.current) return;
    const ob = new ResizeObserver((entries) => {
      for (const btn of entries) {
        setSize({ w: btn.contentRect.width, h: btn.contentRect.height });
      }
    });
    ob.observe(containerRef.current);
    const rect = containerRef.current.getBoundingClientRect();
    setSize({ w: rect.width, h: rect.height });
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
