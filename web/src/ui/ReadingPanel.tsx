import { Check, Copy, RefreshCw, X } from "lucide-react";

interface ReadingPanelProps {
  copied: boolean;
  extractedText: string;
  extractionProgress: string;
  file: File | null;
  isExtracting: boolean;
  lastExtractedPage: number;
  totalPdfPages: number | null;
  onCancelExtraction: () => void;
  onCopy: () => void;
  onNewFile: () => void;
  onResumeExtraction: () => void;
  onStartExtraction: () => void;
}

export function ReadingPanel({
  copied,
  extractedText,
  extractionProgress,
  file,
  isExtracting,
  lastExtractedPage,
  totalPdfPages,
  onCancelExtraction,
  onCopy,
  onNewFile,
  onResumeExtraction,
  onStartExtraction,
}: ReadingPanelProps) {
  return (
    <div className="w-full animate-in fade-in duration-700 pb-20 px-0 sm:px-6 md:px-0">
      <article className="w-full text-gray-900 dark:text-gray-100">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-8">
          <h1 className="text-2xl sm:text-3xl font-bold leading-tight tracking-tight text-gray-950 dark:text-gray-50">
            Извлеченный текст
          </h1>
          <div className="hidden md:flex items-center gap-3">
            <button
              onClick={onStartExtraction}
              className="flex-shrink-0 flex items-center justify-center p-2.5 rounded-xl font-bold transition-all active:scale-95 border bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-750 text-gray-700 dark:text-gray-300 border-gray-200 dark:border-gray-700 shadow-sm"
              title="Переделать"
            >
              <RefreshCw className="w-4 h-4 text-gray-600 dark:text-gray-400" />
            </button>
            <button
              onClick={onCopy}
              className={`flex-shrink-0 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl font-bold text-sm transition-all active:scale-95 border ${
                copied
                  ? "bg-green-50 dark:bg-green-900/30 border-green-200 dark:border-green-800 text-green-700 dark:text-green-400 shadow-sm"
                  : "bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-750 text-gray-700 dark:text-gray-300 border-gray-200 dark:border-gray-700 shadow-sm"
              }`}
            >
              {copied ? (
                <Check className="w-4 h-4" />
              ) : (
                <Copy className="w-4 h-4 text-gray-400 dark:text-gray-500" />
              )}
              {copied ? "Скопировано!" : "Копировать всё"}
            </button>
          </div>
        </div>

        {isExtracting && (
          <div className="flex flex-col sm:flex-row items-center gap-4 py-6 mb-6 justify-center border-b border-gray-100 dark:border-gray-800">
            <div className="flex items-center gap-3 text-blue-600 dark:text-blue-400">
              <div className="w-5 h-5 border-2 border-blue-200 dark:border-blue-900 border-t-blue-600 dark:border-t-blue-400 rounded-full animate-spin" />
              <span className="font-medium text-sm animate-pulse">
                {extractionProgress}
              </span>
            </div>
            <button
              onClick={onCancelExtraction}
              className="px-3 py-1.5 bg-gray-100 hover:bg-red-50 text-gray-600 hover:text-red-600 dark:bg-gray-800 dark:hover:bg-red-900/40 dark:text-gray-400 dark:hover:text-red-400 rounded-lg text-xs font-bold transition-colors flex items-center gap-1"
            >
              <X className="w-3.5 h-3.5" />
              Остановить
            </button>
          </div>
        )}

        <div className="max-w-prose mx-auto text-[17px] sm:text-[18px] leading-[1.8] sm:leading-[2.0] space-y-[24px] sm:space-y-[28px] text-gray-800 dark:text-gray-300 selection:bg-blue-100 dark:selection:bg-blue-900 pb-12 whitespace-pre-wrap font-sans w-full bg-[#fdfcf8] dark:bg-gray-900 p-6 sm:p-10 rounded-2xl shadow-[inset_0_0_20px_rgba(0,0,0,0.02)] border border-gray-100 dark:border-gray-800/60">
          {extractedText}
        </div>

        {file?.type === "application/pdf" &&
        totalPdfPages &&
        lastExtractedPage <= totalPdfPages &&
        !isExtracting ? (
          <div className="mt-8 flex justify-center pb-32 md:pb-12">
            <button
              onClick={onResumeExtraction}
              className="flex items-center gap-2 px-6 py-3 bg-blue-600 hover:bg-blue-700 text-white rounded-xl font-bold transition-all shadow-md active:scale-95"
            >
              <RefreshCw className="w-5 h-5" />
              Продолжить со страницы {lastExtractedPage} из {totalPdfPages}
            </button>
          </div>
        ) : (
          <div className="pb-32 md:pb-12" />
        )}

        <div className="fixed bottom-0 left-0 right-0 p-4 pb-6 bg-gradient-to-t from-gray-50 dark:from-gray-950 via-gray-50/95 dark:via-gray-950/95 to-transparent md:static md:bg-none md:p-0 md:mt-4 md:flex md:flex-row items-center md:border-t md:border-gray-100 dark:md:border-gray-800 md:pt-8 z-30">
          <div className="flex gap-3 w-full mx-auto px-0 md:px-0">
            <button
              onClick={onNewFile}
              className="hidden md:flex w-auto flex-none py-2.5 px-6 text-center text-gray-700 dark:text-gray-300 font-bold bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors shadow-sm text-sm items-center justify-center gap-2 active:scale-95"
            >
              Новый скриншот
            </button>
            <button
              onClick={onStartExtraction}
              className="flex md:hidden w-[52px] h-[52px] flex-shrink-0 text-center text-gray-700 dark:text-gray-300 font-bold bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors shadow-sm items-center justify-center active:scale-95"
              title="Переделать"
            >
              <RefreshCw className="w-5 h-5 text-gray-600 dark:text-gray-400" />
            </button>
            <button
              onClick={onCopy}
              className={`flex-1 flex md:hidden h-[52px] items-center justify-center gap-2 whitespace-nowrap text-center font-bold rounded-xl transition-all shadow-lg text-[15px] active:scale-95 ${
                copied
                  ? "bg-green-500 text-white shadow-green-500/20"
                  : "bg-blue-600 hover:bg-blue-700 text-white shadow-blue-500/30"
              }`}
            >
              {copied ? (
                <>
                  <Check className="w-5 h-5 flex-shrink-0" /> Скопировано
                </>
              ) : (
                <>
                  <Copy className="w-5 h-5 flex-shrink-0" /> Копировать
                </>
              )}
            </button>
          </div>
        </div>
      </article>
    </div>
  );
}
