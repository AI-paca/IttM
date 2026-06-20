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
  selectedSource: SourceType;
  showHeader: boolean;
  onDragOver: (event: DragEvent<HTMLDivElement>) => void;
  onDragLeave: (event: DragEvent<HTMLDivElement>) => void;
  onDrop: (event: DragEvent<HTMLDivElement>, autoStart?: boolean) => void;
  onNewFile: () => void;
  onOpenSidebar: () => void;
  onSourceSelect: (source: SourceType) => void;
}

export function AppHeader({
  appState,
  file,
  isDragging,
  selectedSource,
  showHeader,
  onDragOver,
  onDragLeave,
  onDrop,
  onNewFile,
  onOpenSidebar,
  onSourceSelect,
}: AppHeaderProps) {
  const selectedSourceLabel =
    SOURCES.find((s) => s.id === selectedSource)?.label ?? selectedSource;
  const sourceShortcuts: { id: SourceType; label: string }[] = [
    { id: "auto", label: "Auto" },
    { id: "browser", label: "Browser" },
  ];

  const ghostButton =
    "inline-flex h-7 items-center justify-center rounded-md text-[var(--color-text-muted)] opacity-75 transition-all hover:bg-[var(--color-bg-elevated)]/70 hover:text-[var(--color-text-primary)] hover:opacity-100 active:scale-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent-ring)]";
  const sourceButton = (id: SourceType) =>
    `inline-flex h-7 items-center justify-center rounded-md px-2.5 text-[11px] font-semibold transition-all active:scale-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent-ring)] ${
      selectedSource === id
        ? "border border-[var(--color-border-subtle)] bg-[var(--color-bg-elevated)]/70 text-[var(--color-text-primary)] opacity-95"
        : "text-[var(--color-text-muted)] opacity-58 hover:bg-[var(--color-bg-elevated)]/55 hover:text-[var(--color-text-primary)] hover:opacity-90"
    }`;

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
            className={`mx-auto grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-2 px-3 sm:px-5 md:px-7 ${
              appState === "upload" ? "h-10 md:h-11" : "h-9 md:h-10"
            }`}
          >
            <button
              type="button"
              onClick={() => {
                if (appState !== "upload") onNewFile();
              }}
              className={`min-w-0 justify-self-start text-left ${
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
                    <span className="relative flex h-5 w-5 shrink-0 items-center justify-center">
                      <span className="h-2.5 w-2.5 rounded-full bg-[var(--color-accent)] shadow-[0_0_12px_var(--color-accent)]" />
                      <span className="absolute h-1 w-1 -translate-x-2 rounded-full bg-[var(--color-accent-strong)]/80" />
                    </span>
                    <span className="truncate text-[13px] font-semibold text-[var(--color-text-primary)] md:text-sm">
                      Text Extractor
                    </span>
                    <BugReportLink />
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
                      <FileText className="h-3.5 w-3.5 transition-opacity duration-200 group-hover:opacity-0" />
                      <RefreshCw className="absolute h-3.5 w-3.5 opacity-0 transition-opacity duration-200 group-hover:opacity-100" />
                    </span>
                    <span className="max-w-[150px] truncate text-[12px] font-medium text-[var(--color-text-secondary)] group-hover:text-[var(--color-text-primary)] md:max-w-[260px] md:text-[13px]">
                      {file?.name}
                    </span>
                  </motion.div>
                )}
              </AnimatePresence>
            </button>

            <div className="flex min-w-0 items-center justify-end gap-1.5">
              {appState !== "upload" && (
                <>
                  <div className="hidden items-center gap-1 sm:flex">
                    {sourceShortcuts.map((source) => (
                      <button
                        key={source.id}
                        type="button"
                        onClick={() => onSourceSelect(source.id)}
                        className={sourceButton(source.id)}
                      >
                        {source.label}
                      </button>
                    ))}
                  </div>
                  <button
                    type="button"
                    onClick={onOpenSidebar}
                    className={`${ghostButton} hidden max-w-[170px] px-2 sm:inline-flex`}
                    title={selectedSourceLabel}
                  >
                    <span className="mr-1 h-1.5 w-1.5 shrink-0 rounded-full bg-[var(--color-accent)]" />
                    <span className="truncate text-[11px] font-medium">
                      {selectedSourceLabel}
                    </span>
                  </button>
                </>
              )}
              <button
                type="button"
                onClick={onOpenSidebar}
                className={`${ghostButton} w-7 shrink-0`}
                title="Настройки"
                aria-label="Настройки"
              >
                <Settings className="h-4 w-4" />
              </button>
            </div>
          </div>
        </motion.header>
      )}
    </AnimatePresence>
  );
}
