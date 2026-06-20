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
          <div className="w-6 h-6 border-4 border-blue-100 dark:border-blue-900 border-t-blue-600 dark:border-t-blue-400 rounded-full animate-spin shrink-0"></div>
          <h2 className="text-base sm:text-lg font-semibold text-gray-700 dark:text-gray-200 truncate">
            {extractionProgress}
          </h2>
        </div>
        <button
          onClick={onCancelExtraction}
          className="px-4 py-2 bg-gray-100 hover:bg-red-50 text-gray-600 hover:text-red-600 dark:bg-gray-800 dark:hover:bg-red-900/40 dark:text-gray-400 dark:hover:text-red-400 rounded-lg text-sm font-bold transition-colors flex items-center justify-center gap-2 shrink-0 w-full sm:w-auto"
        >
          <X className="w-4 h-4" />
          Отменить
        </button>
      </div>

      <div className="w-full max-w-4xl mx-auto space-y-6 bg-white dark:bg-gray-800/50 p-5 sm:p-8 rounded-2xl border border-gray-100 dark:border-gray-700/50 shadow-sm mt-2 sm:mt-4">
        <div className="flex items-center gap-3 mb-4 sm:mb-6 border-b border-gray-100 dark:border-gray-700/50 pb-4">
          <div className="text-sm font-medium text-gray-500 dark:text-gray-400">
            Оценка времени завершения:{" "}
            <span className="text-blue-600 dark:text-blue-400">
              ~{estimatedSeconds} сек
            </span>
          </div>
        </div>
        <div className="h-6 sm:h-8 bg-gray-100 dark:bg-gray-700/60 rounded-xl w-3/4 animate-pulse mb-6"></div>
        <div className="space-y-4">
          <div className="h-4 bg-gray-100 dark:bg-gray-700/60 rounded-md w-full animate-pulse"></div>
          <div className="h-4 bg-gray-100 dark:bg-gray-700/60 rounded-md w-5/6 animate-pulse delay-75"></div>
          <div className="h-4 bg-gray-100 dark:bg-gray-700/60 rounded-md w-full animate-pulse delay-100"></div>
          <div className="h-4 bg-gray-100 dark:bg-gray-700/60 rounded-md w-4/5 animate-pulse delay-150"></div>
          <div className="h-4 bg-gray-100 dark:bg-gray-700/60 rounded-md w-11/12 animate-pulse delay-200"></div>
          <div className="h-4 bg-gray-100 dark:bg-gray-700/60 rounded-md w-2/3 animate-pulse delay-300"></div>
        </div>
      </div>
    </div>
  );
}
