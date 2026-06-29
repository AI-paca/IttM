import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { motion } from "motion/react";
import type { ExtractionDocumentProgress } from "../ocr/types";

interface DocumentProgressBarProps {
  progress: ExtractionDocumentProgress | null;
  onCancelExtraction: () => void;
}

function clampPercent(value: number | null): number | null {
  if (value === null || !Number.isFinite(value)) return null;
  return Math.max(0, Math.min(1, value));
}

function pageLabel(progress: ExtractionDocumentProgress | null): string {
  if (!progress?.currentPage) return "Подготовка документа";
  if (progress.totalPages) {
    const current = Math.min(progress.currentPage, progress.totalPages);
    return `Страница ${current} из ${progress.totalPages}`;
  }
  return `Страница ${progress.currentPage}`;
}

const FAST_PROGRESS_RESPONSE_SECONDS = 0.11;
const DEFAULT_PROGRESS_RESPONSE_SECONDS = 0.24;
const CLOSE_PROGRESS_RESPONSE_SECONDS = 0.34;

function progressSoftTarget(
  progress: ExtractionDocumentProgress | null,
  percent: number,
): number {
  if (percent >= 0.995) return 1;
  if (!progress?.totalPages) return percent;

  const pageShare = 1 / progress.totalPages;
  const currentPagePercent = progress.currentPagePercent ?? 0;
  const completedPages = Math.min(progress.completedPages, progress.totalPages);
  const withinPageTarget =
    (completedPages + Math.min(0.96, currentPagePercent + 0.28)) /
    progress.totalPages;
  const gentleLead = Math.min(pageShare * 0.12, 0.026);
  const cappedWithinPageTarget = Math.min(percent + 0.045, withinPageTarget);

  return Math.min(
    0.992,
    Math.max(percent, percent + gentleLead, cappedWithinPageTarget),
  );
}

function responseSecondsForDistance(distance: number) {
  if (distance > 0.12) return FAST_PROGRESS_RESPONSE_SECONDS;
  if (distance > 0.035) return DEFAULT_PROGRESS_RESPONSE_SECONDS;
  return CLOSE_PROGRESS_RESPONSE_SECONDS;
}

function useSmoothedPercent(
  progress: ExtractionDocumentProgress | null,
  targetPercent: number | null,
) {
  const [displayPercent, setDisplayPercent] = useState<number | null>(
    targetPercent,
  );
  const displayRef = useRef<number | null>(targetPercent);
  const frameRef = useRef<number | null>(null);

  useEffect(() => {
    if (frameRef.current !== null) {
      cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    }

    if (targetPercent === null) {
      displayRef.current = null;
      frameRef.current = requestAnimationFrame(() => {
        frameRef.current = null;
        setDisplayPercent(null);
      });
      return () => {
        if (frameRef.current !== null) {
          cancelAnimationFrame(frameRef.current);
          frameRef.current = null;
        }
      };
    }

    if (displayRef.current === null) {
      displayRef.current = targetPercent;
    }

    let previousTime = performance.now();
    const tick = (time: number) => {
      const current = displayRef.current ?? targetPercent;
      const softTarget = Math.max(
        current,
        progressSoftTarget(progress, targetPercent),
      );

      // Clamp elapsed time to absorb tab-switch pauses and avoid giant jumps.
      const elapsedSeconds = Math.min((time - previousTime) / 1000, 0.06);
      previousTime = time;
      const distance = softTarget - current;

      if (distance < 0.0006) {
        displayRef.current = softTarget;
        setDisplayPercent(softTarget);
        return;
      }

      const responseSeconds = responseSecondsForDistance(distance);
      const smoothing = 1 - Math.exp(-elapsedSeconds / responseSeconds);
      const next = clampPercent(current + distance * smoothing) ?? softTarget;
      displayRef.current = next;
      setDisplayPercent(next);
      frameRef.current = requestAnimationFrame(tick);
    };

    frameRef.current = requestAnimationFrame(tick);

    return () => {
      if (frameRef.current !== null) {
        cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }
    };
  }, [progress, targetPercent]);

  return targetPercent === null ? null : (displayPercent ?? targetPercent);
}

/**
 * Линейная экстраполяция «оставшегося времени» по текущему прогрессу.
 *
 *   eta = elapsed * (1 - p) / p
 *
 * При p = 0.5 и прошедших 30с предсказываем ещё 30с. При p → 0 или
 * p → 1 формула расходится/схлопывается, поэтому вводим «мёртвые зоны»:
 *   - p < 0.02 → данных ещё нет, ETA не показываем
 *   - p ≥ 0.995 → почти готово, ETA не показываем
 *
 * Хранит startedAt в ref, чтобы не вызывать лишние ререндеры и не
 * зависеть от системного времени в render-фазе.
 */
function useExtractionEta(displayPercent: number | null): number | null {
  const startedAtRef = useRef<number | null>(null);
  const [eta, setEta] = useState<number | null>(null);

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      if (displayPercent === null || displayPercent < 0.02) {
        startedAtRef.current = null;
        setEta(null);
        return;
      }
      if (startedAtRef.current === null) {
        startedAtRef.current = performance.now();
      }
      const elapsedSeconds = (performance.now() - startedAtRef.current) / 1000;
      if (displayPercent >= 0.995) {
        setEta(0);
        return;
      }
      const predicted =
        (elapsedSeconds * (1 - displayPercent)) / displayPercent;
      setEta(predicted);
    });

    return () => cancelAnimationFrame(frame);
  }, [displayPercent]);

  return eta;
}

function formatEtaLabel(seconds: number | null): string | null {
  if (seconds === null || !Number.isFinite(seconds)) return null;
  if (seconds < 1) return "осталось ~ несколько секунд";
  if (seconds < 60) return `осталось ~ ${Math.max(1, Math.round(seconds))} сек`;
  if (seconds < 3600) return `осталось ~ ${Math.round(seconds / 60)} мин`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return minutes === 0
    ? `осталось ~ ${hours} ч`
    : `осталось ~ ${hours} ч ${minutes} мин`;
}

interface WrapPathLayout {
  d: string;
  width: number;
  height: number;
}

function useCancelWrapPath() {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const [layout, setLayout] = useState<WrapPathLayout | null>(null);

  useEffect(() => {
    const root = rootRef.current;
    const button = buttonRef.current;
    if (!root || !button) return;

    const measure = () => {
      const rootRect = root.getBoundingClientRect();
      const buttonRect = button.getBoundingClientRect();
      const width = Math.max(1, rootRect.width);
      const top = 2.5;
      const buttonRadius = 12;
      const gutter = 3;
      const rawLeft = Math.max(0, buttonRect.left - rootRect.left - gutter);
      const rawRight = Math.min(
        width,
        buttonRect.right - rootRect.left + gutter,
      );
      const bottom = Math.max(top, buttonRect.bottom - rootRect.top + gutter);
      const height = Math.max(36, bottom + 4);
      const radius = Math.min(
        buttonRadius,
        (rawRight - rawLeft) / 2,
        (bottom - top) / 2,
      );
      const left = rawLeft;
      const right = rawRight;
      const pathWidth = Math.max(width, right + radius);
      const curve = radius * 0.5522847498;

      setLayout({
        width: pathWidth,
        height,
        d: [
          `M 0 ${top}`,
          `H ${left}`,
          `V ${bottom - radius}`,
          `C ${left} ${bottom - radius + curve} ${left + radius - curve} ${bottom} ${left + radius} ${bottom}`,
          `H ${right - radius}`,
          `C ${right - radius + curve} ${bottom} ${right} ${bottom - radius + curve} ${right} ${bottom - radius}`,
          `V ${top}`,
          `H ${pathWidth}`,
        ].join(" "),
      });
    };

    measure();
    const frame = requestAnimationFrame(measure);
    const resizeObserver = new ResizeObserver(measure);
    resizeObserver.observe(root);
    resizeObserver.observe(button);
    window.addEventListener("resize", measure);

    return () => {
      cancelAnimationFrame(frame);
      resizeObserver.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, []);

  return { buttonRef, layout, rootRef };
}

export function DocumentProgressBar({
  progress,
  onCancelExtraction,
}: DocumentProgressBarProps) {
  const percent = clampPercent(progress?.documentPercent ?? null);
  const displayPercent = useSmoothedPercent(progress, percent);
  const etaSeconds = useExtractionEta(displayPercent);
  const etaLabel = formatEtaLabel(etaSeconds);
  const showEta =
    etaLabel !== null && displayPercent !== null && displayPercent < 0.995;
  const percentLabel =
    displayPercent === null ? "—" : `${Math.round(displayPercent * 100)}%`;
  const width =
    displayPercent === null
      ? "8%"
      : `${Math.max(2, displayPercent * 100).toFixed(2)}%`;
  const pathProgress = displayPercent ?? 0.08;
  const { buttonRef, layout, rootRef } = useCancelWrapPath();

  return (
    <motion.div
      layout
      initial={false}
      exit={{
        opacity: 0,
        y: -3,
        transition: { duration: 0.1, ease: "easeOut" },
      }}
      transition={{ duration: 0.16, ease: "easeOut" }}
      ref={rootRef}
      className="document-progress-chrome relative min-h-[48px] w-full min-w-0 border-b border-[var(--color-border-subtle)] px-4 pb-3 pr-12 pt-[17px] sm:min-h-[52px] sm:px-6 sm:pr-32 sm:pt-5"
    >
      {layout ? (
        <svg
          className="pointer-events-none absolute left-0 top-0 z-30 overflow-visible"
          width={layout.width}
          height={layout.height}
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          aria-hidden="true"
        >
          <path className="progress-edge-track-path" d={layout.d} />
          <path
            className={`progress-edge-fill-path ${
              percent === null ? "animate-pulse" : ""
            }`}
            d={layout.d}
            pathLength={1}
            style={{
              strokeDasharray: 1,
              strokeDashoffset: Math.max(0, 1 - pathProgress),
            }}
          />
        </svg>
      ) : (
        <div className="progress-edge-track pointer-events-none absolute inset-x-0 top-0 h-[5px] overflow-hidden">
          <div
            className={`progress-edge-fill absolute inset-y-0 left-0 ${
              percent === null ? "animate-pulse" : ""
            }`}
            style={{ width }}
          />
        </div>
      )}
      <button
        ref={buttonRef}
        onClick={onCancelExtraction}
        title="Отменить извлечение"
        aria-label="Отменить извлечение"
        className="cancel-wrap-button absolute right-3 top-[2px] z-20 flex h-9 w-9 items-center justify-center gap-1.5 overflow-hidden rounded-xl border border-transparent bg-transparent text-[11px] font-semibold text-[var(--color-text-secondary)] shadow-none transition-colors active:scale-95 sm:right-4 sm:min-w-[118px] sm:px-3.5"
      >
        <span className="relative z-10 inline-flex items-center gap-1.5">
          <X className="w-3 h-3" />
          <span className="hidden sm:inline">Отменить</span>
        </span>
      </button>
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs sm:text-sm">
        <span className="font-semibold tabular-nums text-[var(--color-accent-strong)] whitespace-nowrap">
          {percentLabel}
        </span>
        <span className="min-w-0 truncate font-medium text-[var(--color-text-secondary)]">
          {pageLabel(progress)}
        </span>
        {showEta && (
          <span className="hidden min-w-0 truncate text-[var(--color-text-muted)] sm:inline">
            ({etaLabel})
          </span>
        )}
      </div>
    </motion.div>
  );
}
