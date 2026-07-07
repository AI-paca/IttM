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

export function DocumentProgressBar({ progress }: DocumentProgressBarProps) {
  const percent = clampPercent(progress?.documentPercent ?? null);
  const percentLabel =
    percent === null
      ? "ожидаем число страниц"
      : `${Math.round(percent * 100)}%`;
  const width =
    percent === null ? "38%" : `${Math.max(4, Math.round(percent * 100))}%`;

  return (
    <div className="w-full">
      <div className="mb-2 flex items-center justify-between gap-3 text-xs sm:text-sm">
        <span className="font-medium text-[var(--color-text-secondary)]">
          {pageLabel(progress)}
        </span>
        <span className="text-[var(--color-info)]">{percentLabel}</span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-[var(--color-bg-muted)]">
        <div
          className={`h-full rounded-full bg-[var(--color-info)] transition-[width] duration-500 ${
            percent === null ? "animate-pulse" : ""
          }`}
          style={{ width }}
        />
      </div>
    </div>
  );
}
