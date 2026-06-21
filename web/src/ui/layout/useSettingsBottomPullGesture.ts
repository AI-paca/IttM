import { useEffect, useRef } from "react";

interface UseSettingsBottomPullGestureArgs {
  enabled: boolean;
  onOpen: () => void;
}

interface PullStart {
  x: number;
  y: number;
  time: number;
}

type InputKind = "pointer" | "touch";

const MOBILE_QUERY = "(max-width: 639px), (pointer: coarse)";
const BOTTOM_ZONE_PX = 180;
const SYSTEM_BOTTOM_GUARD_PX = 16;
const OPEN_DISTANCE_PX = 72;
const MAX_HORIZONTAL_DRIFT_PX = 84;
const MAX_DURATION_MS = 1000;

function isBlockingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  return Boolean(
    target.closest('a, input, textarea, select, [contenteditable="true"]'),
  );
}

function isBottomStart(y: number): boolean {
  const height = window.innerHeight;
  return y >= height - BOTTOM_ZONE_PX && y <= height - SYSTEM_BOTTOM_GUARD_PX;
}

function isAtPageTop(): boolean {
  return window.scrollY <= 2 && document.documentElement.scrollTop <= 2;
}

export function useSettingsBottomPullGesture({
  enabled,
  onOpen,
}: UseSettingsBottomPullGestureArgs) {
  const onOpenRef = useRef(onOpen);

  useEffect(() => {
    onOpenRef.current = onOpen;
  }, [onOpen]);

  useEffect(() => {
    if (typeof window === "undefined") return;

    const mediaQuery = window.matchMedia(MOBILE_QUERY);
    let start: PullStart | null = null;
    let activeInput: InputKind | null = null;
    let opened = false;

    const reset = (kind?: InputKind) => {
      if (kind && activeInput && activeInput !== kind) return;
      start = null;
      activeInput = null;
      opened = false;
    };

    const canListen = () => enabled && mediaQuery.matches;

    const begin = (
      x: number,
      y: number,
      target: EventTarget | null,
      kind: InputKind,
    ) => {
      if (!canListen()) return;
      if (!isAtPageTop()) return;
      if (!isBottomStart(y)) return;
      if (isBlockingTarget(target)) return;

      start = {
        x,
        y,
        time: performance.now(),
      };
      activeInput = kind;
      opened = false;
    };

    const move = (x: number, y: number, kind: InputKind, event?: Event) => {
      if (!start || opened || !canListen()) return;
      if (activeInput !== kind) return;

      const deltaX = Math.abs(x - start.x);
      const deltaY = start.y - y;
      const elapsed = performance.now() - start.time;

      if (
        deltaY < -12 ||
        deltaX > MAX_HORIZONTAL_DRIFT_PX ||
        elapsed > MAX_DURATION_MS ||
        !isAtPageTop()
      ) {
        reset(kind);
        return;
      }

      if (deltaY > 8 && event?.cancelable) {
        event.preventDefault();
      }

      if (deltaY >= OPEN_DISTANCE_PX) {
        opened = true;
        onOpenRef.current();
        start = null;
        activeInput = null;
      }
    };

    const onPointerDown = (event: PointerEvent) => {
      if (event.pointerType && event.pointerType !== "touch") return;
      begin(event.clientX, event.clientY, event.target, "pointer");
    };

    const onPointerMove = (event: PointerEvent) => {
      if (event.pointerType && event.pointerType !== "touch") return;
      move(event.clientX, event.clientY, "pointer", event);
    };

    const onTouchStart = (event: TouchEvent) => {
      const touch = event.touches[0];
      if (!touch) return;
      begin(touch.clientX, touch.clientY, event.target, "touch");
    };

    const onTouchMove = (event: TouchEvent) => {
      const touch = event.touches[0];
      if (!touch) return;
      move(touch.clientX, touch.clientY, "touch", event);
    };

    const onPointerEnd = () => reset("pointer");
    const onTouchEnd = () => reset("touch");

    window.addEventListener("pointerdown", onPointerDown, { passive: true });
    window.addEventListener("pointermove", onPointerMove, { passive: true });
    window.addEventListener("pointerup", onPointerEnd, { passive: true });
    window.addEventListener("pointercancel", onPointerEnd, { passive: true });
    window.addEventListener("touchstart", onTouchStart, { passive: true });
    window.addEventListener("touchmove", onTouchMove, { passive: false });
    window.addEventListener("touchend", onTouchEnd, { passive: true });
    window.addEventListener("touchcancel", onTouchEnd, { passive: true });

    return () => {
      window.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerEnd);
      window.removeEventListener("pointercancel", onPointerEnd);
      window.removeEventListener("touchstart", onTouchStart);
      window.removeEventListener("touchmove", onTouchMove);
      window.removeEventListener("touchend", onTouchEnd);
      window.removeEventListener("touchcancel", onTouchEnd);
    };
  }, [enabled]);
}
