import type { DragEvent } from "react";
import { FileText, RefreshCw, Settings } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import type { AppState } from "../types/app.types";
import type { SourceType } from "../ocr/types";
import { BugReportLink } from "./BugReportLink";
import { SOURCES } from "./sources";

interface AppHeaderProps {
  appState: AppState;
  file: File | null;
  isDragging: boolean;
  isExtracting: boolean;
  selectedSource: SourceType;
  showHeader: boolean;
  onDragOver: (event: DragEvent<HTMLDivElement>) => void;
  onDragLeave: (event: DragEvent<HTMLDivElement>) => void;
  onDrop: (event: DragEvent<HTMLDivElement>, autoStart?: boolean) => void;
  onNewFile: () => void;
  onOpenSidebar: () => void;
}

export function AppHeader({
  appState,
  file,
  isDragging,
  isExtracting,
  selectedSource,
  showHeader,
  onDragOver,
  onDragLeave,
  onDrop,
  onNewFile,
  onOpenSidebar,
}: AppHeaderProps) {
  const selectedSourceLabel =
    SOURCES.find((s) => s.id === selectedSource)?.label ?? selectedSource;
  const fileLabel =
    file?.name ?? (appState === "loading" ? "Документ обрабатывается" : "");

  const ghostButton =
    "inline-flex items-center justify-center rounded-md text-[var(--color-text-muted)] opacity-75 transition-all hover:bg-[var(--color-bg-elevated)]/70 hover:text-[var(--color-text-primary)] hover:opacity-100 active:scale-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent-ring)]";
  const settingsButton = `${ghostButton} absolute inset-y-0 right-0 z-10 w-1/4 min-w-[104px] justify-end rounded-none pr-4 sm:static sm:min-w-0 sm:w-7 sm:h-7 sm:rounded-md sm:p-0`;

  return (
    <AnimatePresence>
      {showHeader && (
        <motion.header
          initial={{ y: 0 }}
          animate={{ y: 0 }}
          exit={{ y: -56 }}
          transition={{ duration: 0.24, ease: "easeInOut" }}
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}
          onDrop={(event) => onDrop(event as DragEvent<HTMLDivElement>, true)}
          className={`sticky top-0 z-40 w-full border-b transition-colors duration-300 ${
            isDragging && appState !== "upload"
              ? "border-[var(--color-info-border)] bg-[var(--color-info-soft)]/55"
              : "border-[var(--color-border-subtle)]/55 bg-[var(--color-bg-app)]/58"
          } backdrop-blur-md`}
        >
          <div
            className={`relative mx-auto grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-2 pl-3 pr-2 sm:px-5 md:px-7 ${
              appState === "upload"
                ? "h-[52px] sm:h-10 md:h-11"
                : "h-[52px] sm:h-9 md:h-10"
            }`}
          >
            <button
              type="button"
              onClick={() => {
                if (appState !== "upload") onNewFile();
              }}
              className={`-ml-1.5 flex h-11 min-w-[128px] max-w-[72vw] items-center justify-self-start rounded-md px-1.5 text-left sm:ml-0 sm:h-auto sm:min-w-0 sm:max-w-none sm:px-0 ${
                appState !== "upload"
                  ? "cursor-pointer hover:opacity-100"
                  : "cursor-default"
              } opacity-85 transition-opacity`}
              title={appState !== "upload" ? "Новый скриншот" : undefined}
            >
              <AnimatePresence mode="popLayout" initial={false}>
                {appState === "upload" ? (
                  <motion.div
                    key="logo-view"
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    exit={{ opacity: 0, x: -8 }}
                    transition={{ duration: 0.2 }}
                    className="flex min-w-0 items-center gap-2"
                  >
                    <span className="truncate text-[13px] font-semibold text-[var(--color-text-primary)] md:text-sm">
                      Text Extractor
                    </span>
                    <BugReportLink className="hidden sm:inline-flex" />
                  </motion.div>
                ) : (
                  <motion.div
                    key="file-view"
                    layoutId="file-upload-zone"
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    exit={{ opacity: 0, x: -8 }}
                    transition={{ duration: 0.2 }}
                    className="group flex min-w-0 items-center gap-2"
                  >
                    <span className="relative flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-[var(--color-text-muted)]">
                      <AnimatePresence mode="popLayout" initial={false}>
                        {isExtracting ? (
                          <motion.span
                            key="spinner"
                            initial={{ opacity: 0, scale: 0.6 }}
                            animate={{ opacity: 1, scale: 1 }}
                            exit={{ opacity: 0, scale: 0.6 }}
                            transition={{ duration: 0.18 }}
                            className="absolute inset-0 flex items-center justify-center text-[var(--color-accent)]"
                            aria-label="Идёт извлечение"
                          >
                            <span className="spinner !h-3.5 !w-3.5 !border-2" />
                          </motion.span>
                        ) : (
                          <motion.span
                            key="file-icon"
                            initial={{ opacity: 0, scale: 0.6 }}
                            animate={{ opacity: 1, scale: 1 }}
                            exit={{ opacity: 0, scale: 0.6 }}
                            transition={{ duration: 0.18 }}
                            className="absolute inset-0 flex items-center justify-center"
                          >
                            <FileText className="h-3.5 w-3.5 transition-opacity duration-200 group-hover:opacity-0" />
                            <RefreshCw className="absolute h-3.5 w-3.5 opacity-0 transition-opacity duration-200 group-hover:opacity-100" />
                          </motion.span>
                        )}
                      </AnimatePresence>
                    </span>
                    <span className="max-w-[150px] truncate text-[12px] font-medium text-[var(--color-text-secondary)] group-hover:text-[var(--color-text-primary)] md:max-w-[260px] md:text-[13px]">
                      {fileLabel}
                    </span>
                  </motion.div>
                )}
              </AnimatePresence>
            </button>

            <div className="flex min-w-0 items-center justify-end gap-1.5">
              {appState !== "upload" && (
                <div
                  className="hidden max-w-[180px] items-center gap-1.5 truncate px-1.5 text-[11px] font-medium text-[var(--color-text-muted)] opacity-65 sm:flex"
                  title={selectedSourceLabel}
                >
                  <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-[var(--color-accent)] opacity-80" />
                  <span className="truncate">{selectedSourceLabel}</span>
                </div>
              )}
              <button
                type="button"
                onClick={onOpenSidebar}
                className={settingsButton}
                title="Настройки"
                aria-label="Настройки"
              >
                <Settings className="h-5 w-5 sm:h-4 sm:w-4" />
              </button>
            </div>
          </div>
        </motion.header>
      )}
    </AnimatePresence>
  );
}
