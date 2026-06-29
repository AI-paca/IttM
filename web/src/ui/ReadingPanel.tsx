import { useEffect, useRef, useState } from "react";
import { Check, Copy, Maximize2, Minimize2, RefreshCw } from "lucide-react";
import { AnimatePresence, LayoutGroup, motion } from "motion/react";
import type { ExtractionDocumentProgress } from "../ocr/types";
import { DocumentProgressBar } from "./DocumentProgressBar";
import { MarkdownContent } from "./MarkdownContent";

interface ReadingPanelProps {
  copied: boolean;
  documentProgress: ExtractionDocumentProgress | null;
  extractedText: string;
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

function useIsDesktopViewport() {
  const [isDesktop, setIsDesktop] = useState(() =>
    typeof window === "undefined"
      ? true
      : window.matchMedia("(min-width: 768px)").matches,
  );

  useEffect(() => {
    const media = window.matchMedia("(min-width: 768px)");
    const update = () => setIsDesktop(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  return isDesktop;
}

/**
 * Единая панель извлечения для 1/3, 2/3, 3/3+.
 *
 *   - Никакой дублирующей шапки: spinner и текст статуса живут в
 *     AppHeader (иконка файла временно заменяется на спиннер) и
 *     встроены в саму карточку через `DocumentProgressBar`.
 *   - Кнопка «Отменить» встроена в DocumentProgressBar как tab на верхней
 *     грани; SVG-path прогресса огибает её периметр.
 *   - Число страниц и оценка времени — не дублируются; их показывает
 *     `DocumentProgressBar` (label «Страница X из Y» + процент).
 *   - Loading использует компактную карточку, а reading — полноценную
 *     документную область; top chrome остаётся общим.
 */
export function ReadingPanel({
  copied,
  documentProgress,
  extractedText,
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
  const showContent = extractedText.length > 0;
  const documentSurfaceRef = useRef<HTMLDivElement | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const isDesktopViewport = useIsDesktopViewport();
  const desktopCopyLayoutId =
    showContent && !isExtracting && isDesktopViewport
      ? "ocr-primary-action"
      : undefined;
  const mobileCopyLayoutId =
    showContent && !isExtracting && !isDesktopViewport
      ? "ocr-primary-action"
      : undefined;
  const primaryActionTransition = {
    type: "spring" as const,
    stiffness: 420,
    damping: 38,
    mass: 0.8,
  };
  const cardLayoutClass = showContent
    ? "max-w-[1280px] min-h-[62svh] md:min-h-[66svh] pb-12"
    : "max-w-[1280px] min-h-[430px] sm:min-h-[500px] lg:min-h-[560px] xl:min-h-[600px] pb-0";
  const contentPaddingClass = showContent
    ? "p-5 pr-16 sm:p-9 sm:pr-20 lg:p-10 lg:pr-24"
    : "px-5 py-5 sm:px-8 sm:py-6 lg:px-10 lg:py-7";
  const fullscreenTitle = isFullscreen
    ? "Выйти из полноэкранного режима"
    : "Во весь экран";

  useEffect(() => {
    const syncFullscreenState = () => {
      setIsFullscreen(
        document.fullscreenElement === documentSurfaceRef.current,
      );
    };

    syncFullscreenState();
    document.addEventListener("fullscreenchange", syncFullscreenState);
    return () =>
      document.removeEventListener("fullscreenchange", syncFullscreenState);
  }, []);

  const toggleFullscreen = async () => {
    const surface = documentSurfaceRef.current;
    if (!surface) return;

    try {
      if (document.fullscreenElement === surface) {
        await document.exitFullscreen();
      } else {
        await surface.requestFullscreen();
      }
    } catch {
      // Fullscreen can be denied by the browser; the UI should simply stay put.
    }
  };

  return (
    <LayoutGroup id="ocr-primary-action-group">
      <div className="w-full animate-in fade-in duration-700 pb-20 px-0 sm:px-6 md:px-0">
        <article className="w-full text-[var(--color-text-primary)]">
          {/* Заголовок + действия — только когда текст уже есть. */}
          {showContent && (
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-4 sm:mb-6">
              <h1 className="text-2xl sm:text-3xl font-bold leading-tight tracking-tight text-[var(--color-text-primary)]">
                Извлеченный текст
              </h1>
              {!isExtracting && (
                <div className="hidden md:flex items-center gap-3">
                  <button
                    onClick={onStartExtraction}
                    className="btn-outline flex-shrink-0 p-2.5"
                    title="Переделать"
                  >
                    <RefreshCw className="w-4 h-4 text-[var(--color-text-secondary)]" />
                  </button>
                  <motion.button
                    layoutId={desktopCopyLayoutId}
                    transition={primaryActionTransition}
                    initial={{ opacity: 0, y: -6 }}
                    animate={{ opacity: 1, y: 0 }}
                    data-copy-action="true"
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
                  </motion.button>
                </div>
              )}
            </div>
          )}

          <div
            ref={documentSurfaceRef}
            className={`document-fullscreen-surface surface-card relative overflow-hidden mx-auto w-full ${cardLayoutClass} text-[16.5px] sm:text-[17px] lg:text-[18px] leading-[1.75] sm:leading-[1.9] text-[var(--color-text-secondary)] selection:bg-[var(--color-info-soft)] font-sans transition-[max-width,min-height,padding] duration-500 ease-out`}
          >
            <AnimatePresence mode="popLayout">
              {isExtracting && (
                <DocumentProgressBar
                  key="document-progress"
                  progress={documentProgress}
                  onCancelExtraction={onCancelExtraction}
                />
              )}
            </AnimatePresence>

            <button
              type="button"
              onClick={toggleFullscreen}
              title={fullscreenTitle}
              aria-label={fullscreenTitle}
              className={`document-fullscreen-toggle absolute z-40 flex h-9 w-9 items-center justify-center rounded-xl border border-transparent bg-transparent text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-elevated)] hover:text-[var(--color-accent-strong)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent-ring)] active:scale-95 ${
                isExtracting
                  ? "right-14 top-[8px] sm:right-[148px]"
                  : "right-4 top-4"
              }`}
            >
              {isFullscreen ? (
                <Minimize2 className="h-4 w-4" />
              ) : (
                <Maximize2 className="h-4 w-4" />
              )}
            </button>

            <div className={contentPaddingClass}>
              {showContent ? (
                <MarkdownContent>{extractedText}</MarkdownContent>
              ) : (
                <div className="space-y-4">
                  <div className="skeleton h-6 sm:h-8 rounded-xl w-3/4 mb-6" />
                  <div className="skeleton h-4 w-full" />
                  <div className="skeleton h-4 w-5/6" />
                  <div className="skeleton h-4 w-full" />
                  <div className="skeleton h-4 w-4/5" />
                  <div className="skeleton h-4 w-11/12" />
                  <div className="skeleton h-4 w-2/3" />
                </div>
              )}
            </div>
          </div>

          {/* Кнопка «Продолжить со страницы X» для PDF с непрочитанными
            страницами. Резервируем высоту, чтобы геометрия не
            «прыгала». */}
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

          {/* Нижняя панель действий (мобильный вариант). */}
          {showContent && !isExtracting && (
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
                <motion.button
                  layoutId={mobileCopyLayoutId}
                  transition={primaryActionTransition}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  data-copy-action="true"
                  onClick={onCopy}
                  className={`flex-1 flex md:hidden h-[52px] items-center justify-center gap-2 whitespace-nowrap text-center font-bold rounded-xl transition-all shadow-lg text-[15px] active:scale-95 ${
                    copied
                      ? "bg-[var(--color-success)] text-white"
                      : "btn-primary"
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
                </motion.button>
              </div>
            </div>
          )}
        </article>
      </div>
    </LayoutGroup>
  );
}
