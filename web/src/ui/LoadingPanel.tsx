import { X } from "lucide-react";

interface LoadingPanelProps {
  extractionProgress: string;
  onCancelExtraction: () => void;
}

export function LoadingPanel({
  extractionProgress,
  onCancelExtraction,
}: LoadingPanelProps) {
  return (
    <div className="flex flex-col w-full animate-in fade-in duration-500">
      <div className="flex items-center justify-between mb-10 w-full gap-4">
        <div className="flex items-center gap-3">
          <div className="w-6 h-6 border-4 border-blue-100 dark:border-blue-900 border-t-blue-600 dark:border-t-blue-400 rounded-full animate-spin"></div>
          <h2 className="text-lg font-semibold text-gray-700 dark:text-gray-200">
            {extractionProgress}
          </h2>
        </div>
        <button
          onClick={onCancelExtraction}
          className="px-4 py-2 bg-gray-100 hover:bg-red-50 text-gray-600 hover:text-red-600 dark:bg-gray-800 dark:hover:bg-red-900/40 dark:text-gray-400 dark:hover:text-red-400 rounded-lg text-sm font-bold transition-colors flex items-center gap-2"
        >
          <X className="w-4 h-4" />
          Отменить
        </button>
      </div>

      <div className="w-full space-y-6">
        <div className="h-8 bg-gray-200 dark:bg-gray-800 rounded-md w-3/4 animate-pulse"></div>
        <div className="space-y-4">
          <div className="h-4 bg-gray-200 dark:bg-gray-800 rounded w-full animate-pulse"></div>
          <div className="h-4 bg-gray-200 dark:bg-gray-800 rounded w-full animate-pulse delay-75"></div>
          <div className="h-4 bg-gray-200 dark:bg-gray-800 rounded w-11/12 animate-pulse delay-100"></div>
        </div>
      </div>
    </div>
  );
}
