import { useCallback, useEffect, useRef, useState } from "react";
import type { TouchEvent, UIEvent, WheelEvent } from "react";
import { flushSync } from "react-dom";

const REVEAL_THRESHOLD = 60;
const MAX_OVERSCROLL = 120;

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
 * Reveal сам добавляет высоту внизу scroll-area. Основной контент остаётся
 * min-h-full, поэтому гибкое пространство над темой не сжимается, а при
 * переполнении вся нижняя секция продолжает уезжать вниз естественным скроллом.
 */
export function useOverscrollReveal(scrollNode: HTMLDivElement | null): {
  overscroll: number;
  isRevealed: boolean;
  touchActive: boolean;
  handlers: OverscrollRevealHandlers;
  close: () => void;
} {
  const [overscroll, setOverscroll] = useState(0);
  const [isRevealed, setIsRevealed] = useState(false);
  const [touchStart, setTouchStart] = useState(0);

  const scrollNodeRef = useRef<HTMLDivElement | null>(scrollNode);
  const isRevealedRef = useRef(isRevealed);
  const overscrollRef = useRef(overscroll);
  const revealSettlingRef = useRef(false);
  const wheelTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    scrollNodeRef.current = scrollNode;
  }, [scrollNode]);

  useEffect(() => {
    isRevealedRef.current = isRevealed;
  }, [isRevealed]);

  const setOverscrollValue = useCallback((value: number) => {
    overscrollRef.current = value;
    setOverscroll(value);
  }, []);

  const scrollToBottom = useCallback(() => {
    const el = scrollNodeRef.current;
    if (!el) return;
    el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight);
  }, []);

  useEffect(() => {
    if (!isRevealed) return;
    let frame = 0;
    let timeout: ReturnType<typeof setTimeout> | null = null;
    revealSettlingRef.current = true;
    const started = performance.now();
    const followReveal = (now: number) => {
      scrollToBottom();
      if (now - started < 320) {
        frame = requestAnimationFrame(followReveal);
      }
    };
    frame = requestAnimationFrame(followReveal);
    timeout = setTimeout(() => {
      revealSettlingRef.current = false;
      scrollToBottom();
    }, 340);
    return () => {
      revealSettlingRef.current = false;
      cancelAnimationFrame(frame);
      if (timeout) clearTimeout(timeout);
    };
  }, [isRevealed, scrollToBottom]);

  useEffect(() => {
    return () => {
      if (wheelTimeoutRef.current) clearTimeout(wheelTimeoutRef.current);
    };
  }, []);

  const closeIfAwayFromBottom = useCallback(() => {
    const el = scrollNodeRef.current;
    if (!el) return;
    const bottomOffset = el.scrollHeight - el.clientHeight - el.scrollTop;
    if (bottomOffset > 4) {
      setIsRevealed(false);
    }
  }, []);

  const closeAfterNativeScroll = useCallback(() => {
    requestAnimationFrame(() => {
      if (isRevealedRef.current) {
        closeIfAwayFromBottom();
      }
    });
  }, [closeIfAwayFromBottom]);

  const reveal = useCallback(() => {
    flushSync(() => {
      setOverscrollValue(0);
      setIsRevealed(true);
    });
  }, [setOverscrollValue]);

  const close = useCallback(() => {
    flushSync(() => {
      setOverscrollValue(0);
      setIsRevealed(false);
    });
  }, [setOverscrollValue]);

  const handleWheelDelta = useCallback(
    (deltaY: number) => {
      const el = scrollNodeRef.current;
      if (!el) return;

      if (isRevealedRef.current) {
        if (deltaY < 0) closeAfterNativeScroll();
        return;
      }

      const maxScroll = el.scrollHeight - el.clientHeight;
      const atBottom = el.scrollTop >= maxScroll - 1;

      if (atBottom && deltaY > 0) {
        const next = Math.min(
          overscrollRef.current + deltaY * 0.5,
          MAX_OVERSCROLL,
        );
        if (next >= REVEAL_THRESHOLD) {
          reveal();
          return;
        }
        setOverscrollValue(next);
      } else if (deltaY < 0) {
        setOverscrollValue(Math.max(0, overscrollRef.current + deltaY * 0.5));
      }

      if (wheelTimeoutRef.current) clearTimeout(wheelTimeoutRef.current);
      wheelTimeoutRef.current = setTimeout(() => {
        if (!isRevealedRef.current) setOverscrollValue(0);
      }, 150);
    },
    [closeAfterNativeScroll, reveal, setOverscrollValue],
  );

  const onWheel = useCallback(
    (e: WheelEvent<HTMLDivElement>) => {
      handleWheelDelta(e.deltaY);
    },
    [handleWheelDelta],
  );

  const onScroll = useCallback((e: UIEvent<HTMLDivElement>) => {
    if (!isRevealedRef.current) return;
    if (revealSettlingRef.current) return;
    const el = e.currentTarget;
    const bottomOffset = el.scrollHeight - el.clientHeight - el.scrollTop;
    if (bottomOffset > 4) {
      setIsRevealed(false);
    }
  }, []);

  const onTouchStart = useCallback((e: TouchEvent<HTMLDivElement>) => {
    setTouchStart(e.touches[0].clientY);
  }, []);

  const onTouchMove = useCallback(
    (e: TouchEvent<HTMLDivElement>) => {
      const el = scrollNodeRef.current;
      if (!el) return;

      const maxScroll = el.scrollHeight - el.clientHeight;
      const deltaY = touchStart - e.touches[0].clientY;
      if (isRevealedRef.current) {
        if (deltaY < -8) setIsRevealed(false);
        return;
      }

      const atBottom = el.scrollTop >= maxScroll - 1;

      setOverscroll((current) => {
        if (atBottom && deltaY > 0) {
          return Math.min(deltaY * 0.8, MAX_OVERSCROLL);
        }
        return current > 0 ? Math.max(0, current + deltaY * 0.8) : 0;
      });
    },
    [touchStart],
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
