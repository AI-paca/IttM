import { useCallback, useEffect, useRef, useState } from "react";
import type { TouchEvent, UIEvent, WheelEvent } from "react";

const REVEAL_THRESHOLD = 60;
const MAX_OVERSCROLL = 120;
const REVEAL_HEIGHT = 120;

interface OverscrollRevealHandlers {
  onWheel: (e: WheelEvent<HTMLDivElement>) => void;
  onScroll: (e: UIEvent<HTMLDivElement>) => void;
  onTouchStart: (e: TouchEvent<HTMLDivElement>) => void;
  onTouchMove: (e: TouchEvent<HTMLDivElement>) => void;
  onTouchEnd: () => void;
}

/**
 * Управляет поведением "pull-to-reveal" для нижней панели сайдбара:
 * при прокрутке вниз за пределы контента накапливает overscroll,
 * и при превышении порога разворачивает дополнительный блок (Issue Tracker).
 *
 * Ключевые отличия от предыдущей реализации:
 * - При раскрытии scrollTop устанавливается ровно в scrollHeight (а не += currentOver),
 *   чтобы reveal-блок оказывался привязан к низу видимой области без «прыжка».
 * - Скрытие происходит по надёжному порогу: reveal сворачивается, только если
 *   пользователь проскроллил вверх дальше, чем высота reveal-блока + запас.
 * - Добавлен явный колбэк close() для кнопки закрытия.
 */
export function useOverscrollReveal(
  scrollRef: React.RefObject<HTMLDivElement | null>,
): {
  overscroll: number;
  isRevealed: boolean;
  touchActive: boolean;
  handlers: OverscrollRevealHandlers;
  close: () => void;
} {
  const [overscroll, setOverscroll] = useState(0);
  const [isRevealed, setIsRevealed] = useState(false);
  const [touchStart, setTouchStart] = useState(0);

  const isRevealedRef = useRef(isRevealed);
  const wheelTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    isRevealedRef.current = isRevealed;
  }, [isRevealed]);

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    // Прыгаем в самый низ, чтобы reveal-блок (последний элемент потока)
    // оказался в видимой области, прижатым к низу.
    el.scrollTop = el.scrollHeight - el.clientHeight;
  }, [scrollRef]);

  const reveal = useCallback(() => {
    setIsRevealed(true);
    setOverscroll(0);
    // Сбрасываем transform (overscroll=0) и прижимаем скролл к низу
    // в следующем кадре — после того, как reveal-блок получит высоту.
    requestAnimationFrame(() => scrollToBottom());
  }, [scrollToBottom]);

  const close = useCallback(() => {
    setIsRevealed(false);
    setOverscroll(0);
  }, []);

  const onWheel = useCallback(
    (e: WheelEvent<HTMLDivElement>) => {
      const el = scrollRef.current;
      if (!el) return;

      // Уже раскрыто — нативный скролл сам управляет позицией,
      // onScroll обработает скрытие при необходимости.
      if (isRevealedRef.current) return;

      const maxScroll = el.scrollHeight - el.clientHeight;
      const atBottom = el.scrollTop >= maxScroll - 1;

      if (atBottom && e.deltaY > 0) {
        // Накапливаем overscroll при прокрутке вниз за пределы контента.
        setOverscroll((prev) =>
          Math.min(prev + e.deltaY * 0.5, MAX_OVERSCROLL),
        );
      } else if (e.deltaY < 0) {
        // Скролл вверх внутри контента гасит накопленный overscroll.
        setOverscroll((prev) => Math.max(0, prev + e.deltaY * 0.5));
      }

      if (wheelTimeoutRef.current) clearTimeout(wheelTimeoutRef.current);
      wheelTimeoutRef.current = setTimeout(() => {
        setOverscroll((latest) => {
          if (!isRevealedRef.current && latest >= REVEAL_THRESHOLD) {
            reveal();
          }
          return 0;
        });
      }, 150);
    },
    [reveal, scrollRef],
  );

  const onScroll = useCallback((e: UIEvent<HTMLDivElement>) => {
    if (!isRevealedRef.current) return;
    const el = e.currentTarget;
    const bottomOffset = el.scrollHeight - el.clientHeight - el.scrollTop;
    // Сворачиваем reveal только если пользователь явно ушёл вверх
    // дальше высоты reveal-блока + запас. Это предотвращает случайное
    // закрытие и позволяет корректно скроллить контент над reveal.
    if (bottomOffset > REVEAL_HEIGHT + 40) {
      setIsRevealed(false);
    }
  }, []);

  const onTouchStart = useCallback((e: TouchEvent<HTMLDivElement>) => {
    setTouchStart(e.touches[0].clientY);
  }, []);

  const onTouchMove = useCallback(
    (e: TouchEvent<HTMLDivElement>) => {
      const el = scrollRef.current;
      if (!el) return;
      if (isRevealedRef.current) return;

      const maxScroll = el.scrollHeight - el.clientHeight;
      const deltaY = touchStart - e.touches[0].clientY;
      const atBottom = el.scrollTop >= maxScroll - 1;

      setOverscroll((current) => {
        if (atBottom && deltaY > 0) {
          return Math.min(deltaY * 0.8, MAX_OVERSCROLL);
        }
        return current > 0 ? Math.max(0, current + deltaY * 0.8) : 0;
      });
    },
    [scrollRef, touchStart],
  );

  const onTouchEnd = useCallback(() => {
    setOverscroll((current) => {
      if (!isRevealedRef.current && current >= REVEAL_THRESHOLD) {
        reveal();
        return 0;
      }
      return 0;
    });
    setTouchStart(0);
  }, [reveal]);

  return {
    overscroll,
    isRevealed,
    touchActive: touchStart > 0,
    handlers: { onWheel, onScroll, onTouchStart, onTouchMove, onTouchEnd },
    close,
  };
}
