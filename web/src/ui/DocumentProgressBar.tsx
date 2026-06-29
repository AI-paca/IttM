import { useEffect, useRef, useState } from "react";
import type { ExtractionDocumentProgress } from "../ocr/types";

interface DocumentProgressBarProps {
  progress: ExtractionDocumentProgress | null;
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
    (completedPages + Math.min(0.92, currentPagePercent + 0.42)) /
    progress.totalPages;
  const gentleLead = Math.min(pageShare * 0.18, 0.035);
  const cappedWithinPageTarget = Math.min(percent + 0.08, withinPageTarget);

  return Math.min(
    0.985,
    Math.max(percent, percent + gentleLead, cappedWithinPageTarget),
  );
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
      setDisplayPercent(null);
      return;
    }

    if (displayRef.current === null) {
      displayRef.current = targetPercent;
      setDisplayPercent(targetPercent);
    }

    let previousTime = performance.now();
    const tick = (time: number) => {
      const current = displayRef.current ?? targetPercent;
      const softTarget = progressSoftTarget(progress, targetPercent);

      const elapsedSeconds = Math.min((time - previousTime) / 1000, 0.08);
      previousTime = time;
      const distance = softTarget - current;

      if (Math.abs(distance) < 0.001) {
        displayRef.current = softTarget;
        setDisplayPercent(softTarget);
        return;
      }

      const smoothing = 1 - Math.exp(-elapsedSeconds * 2.35);
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

  return displayPercent;
}

export function DocumentProgressBar({ progress }: DocumentProgressBarProps) {
  const percent = clampPercent(progress?.documentPercent ?? null);
  const displayPercent = useSmoothedPercent(progress, percent);
  const percentLabel =
    displayPercent === null
      ? "ожидаем число страниц"
      : `${Math.round(displayPercent * 100)}%`;
  const width =
    displayPercent === null
      ? "8%"
      : `${Math.max(2, displayPercent * 100).toFixed(2)}%`;

  return (
    <div className="w-full pt-3">
      <div className="progress-edge-track pointer-events-none absolute inset-x-0 top-0 h-[5px] overflow-hidden rounded-[1px]">
        <div
          className={`progress-edge-fill absolute inset-y-0 left-0 transition-[width] duration-500 ${
            percent === null ? "animate-pulse" : ""
          }`}
          style={{ width }}
        />
      </div>
      <div className="flex items-center justify-between gap-3 text-xs sm:text-sm">
        <span className="font-medium text-[var(--color-text-secondary)]">
          {pageLabel(progress)}
        </span>
        <span className="text-[var(--color-accent-strong)]">
          {percentLabel}
        </span>
      </div>
    </div>
  );
}
