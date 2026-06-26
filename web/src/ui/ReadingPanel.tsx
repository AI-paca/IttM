import { Check, Copy, RefreshCw, X } from "lucide-react";
import type { ExtractionDocumentProgress } from "../ocr/types";
import { DocumentProgressBar } from "./DocumentProgressBar";
import { MarkdownContent } from "./MarkdownContent";

interface ReadingPanelProps {
  copied: boolean;
  documentProgress: ExtractionDocumentProgress | null;
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
  documentProgress,
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
      <article className="w-full text-[var(--color-text-primary)]">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-8">
          <h1 className="text-2xl sm:text-3xl font-bold leading-tight tracking-tight text-[var(--color-text-primary)]">
            Извлеченный текст
          </h1>
          <div className="hidden md:flex items-center gap-3">
            <button
              onClick={onStartExtraction}
              className="btn-outline flex-shrink-0 p-2.5"
              title="Переделать"
            >
              <RefreshCw className="w-4 h-4 text-[var(--color-text-secondary)]" />
            </button>
            <button
              onClick={onCopy}
              className={`flex-shrink-0 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl font-bold text-sm transition-all active:scale-95 border ${
                copied
                  ? "bg-[var(--color-success-soft)] border-[var(--color-success-border)] text-[var(--color-success-text)] shadow-sm"
                  : "btn-outline"
              }`}
            >
              {copied ? (
                <Check className="w-4 h-4" />
              ) : (
                <Copy className="w-4 h-4 text-[var(--color-text-muted)]" />
              )}
              {copied ? "Скопировано!" : "Копировать всё"}
            </button>
          </div>
        </div>

        {isExtracting && (
          <div className="mb-6 border-b border-[var(--color-border-subtle)] py-6">
            <div className="mb-4 flex flex-col sm:flex-row items-center gap-4 justify-center">
              <div className="flex items-center gap-3 text-[var(--color-info)]">
                <div className="spinner w-5 h-5" />
                <span className="font-medium text-sm animate-pulse">
                  {extractionProgress}
                </span>
              </div>
              <button
                onClick={onCancelExtraction}
                className="btn-danger px-3 py-1.5 rounded-lg text-xs"
              >
                <X className="w-3.5 h-3.5" />
                Остановить
              </button>
            </div>
            <DocumentProgressBar progress={documentProgress} />
          </div>
        )}

        <div className="mx-auto w-full max-w-[1120px] min-h-[58svh] md:min-h-[60svh] text-[16.5px] sm:text-[17px] lg:text-[18px] leading-[1.75] sm:leading-[1.9] text-[var(--color-text-secondary)] selection:bg-[var(--color-info-soft)] pb-12 font-sans bg-[var(--color-bg-surface)] p-5 sm:p-9 lg:p-10 rounded-2xl border border-[var(--color-border-subtle)]">
          <MarkdownContent>{extractedText}</MarkdownContent>
        </div>

        {file?.type === "application/pdf" &&
        totalPdfPages &&
        lastExtractedPage <= totalPdfPages &&
        !isExtracting ? (
          <div className="mt-8 flex justify-center pb-32 md:pb-12">
            <button
              onClick={onResumeExtraction}
              className="btn-primary px-6 py-3 text-base shadow-md"
            >
              <RefreshCw className="w-5 h-5" />
              Продолжить со страницы {lastExtractedPage} из {totalPdfPages}
            </button>
          </div>
        ) : (
          <div className="pb-32 md:pb-12" />
        )}

        <div className="fixed bottom-0 left-0 right-0 p-4 pb-6 bg-gradient-to-t from-[var(--color-bg-app)] via-[var(--color-bg-app)]/95 to-transparent md:static md:bg-none md:p-0 md:mt-4 md:flex md:flex-row items-center md:border-t md:border-[var(--color-border-subtle)] md:pt-8 z-30">
          <div className="flex gap-3 w-full mx-auto px-0 md:px-0">
            <button
              onClick={onNewFile}
              className="btn-outline hidden md:flex w-auto flex-none py-2.5 px-6 text-sm"
            >
              Новый скриншот
            </button>
            <button
              onClick={onStartExtraction}
              className="btn-outline flex md:hidden w-[52px] h-[52px] flex-shrink-0 items-center justify-center"
              title="Переделать"
            >
              <RefreshCw className="w-5 h-5 text-[var(--color-text-secondary)]" />
            </button>
            <button
              onClick={onCopy}
              className={`flex-1 flex md:hidden h-[52px] items-center justify-center gap-2 whitespace-nowrap text-center font-bold rounded-xl transition-all shadow-lg text-[15px] active:scale-95 ${
                copied ? "bg-[var(--color-success)] text-white" : "btn-primary"
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
