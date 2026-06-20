import { useState } from "react";
import { X } from "lucide-react";

interface LoadingPanelProps {
  extractionProgress: string;
  onCancelExtraction: () => void;
}

export function LoadingPanel({
  extractionProgress,
  onCancelExtraction,
}: LoadingPanelProps) {
  // Оценка генерируется один раз при монтировании (3–7 сек),
  // чтобы не нарушать чистоту рендера и не дёргать Math.random при каждом рендере.
  const [estimatedSeconds] = useState(() => Math.floor(Math.random() * 5) + 3);

  return (
    <div className="flex flex-col w-full animate-in fade-in duration-500">
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between mb-6 sm:mb-10 w-full gap-4">
        <div className="flex items-center gap-3 min-w-0 w-full">
          <div className="spinner w-6 h-6 shrink-0" />
          <h2 className="text-base sm:text-lg font-semibold text-[var(--color-text-primary)] truncate">
            {extractionProgress}
          </h2>
        </div>
        <button
          onClick={onCancelExtraction}
          className="btn-danger px-4 py-2 rounded-lg text-sm w-full sm:w-auto"
        >
          <X className="w-4 h-4" />
          Отменить
        </button>
      </div>

      <div className="surface-card w-full max-w-4xl mx-auto space-y-6 p-5 sm:p-8 mt-2 sm:mt-4">
        <div className="flex items-center gap-3 mb-4 sm:mb-6 border-b border-[var(--color-border-subtle)] pb-4">
          <div className="text-sm font-medium text-[var(--color-text-secondary)]">
            Оценка времени завершения:{" "}
            <span className="text-[var(--color-info)]">
              ~{estimatedSeconds} сек
            </span>
          </div>
        </div>
        <div className="skeleton h-6 sm:h-8 rounded-xl w-3/4 mb-6" />
        <div className="space-y-4">
          <div className="skeleton h-4 w-full" />
          <div className="skeleton h-4 w-5/6" />
          <div className="skeleton h-4 w-full" />
          <div className="skeleton h-4 w-4/5" />
          <div className="skeleton h-4 w-11/12" />
          <div className="skeleton h-4 w-2/3" />
        </div>
      </div>
    </div>
  );
}