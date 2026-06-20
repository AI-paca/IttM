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
  const btnClass = (id: string) =>
    `px-3 py-1.5 text-xs sm:text-sm font-bold rounded-xl transition-all shadow-sm border ${
      selectedSource === id
        ? "bg-[var(--color-info-soft)] text-[var(--color-info-text)] border-[var(--color-info-border)]"
        : "bg-[var(--color-bg-surface)] text-[var(--color-text-secondary)] border-[var(--color-border-default)] hover:border-[var(--color-info-border)]"
    }`;

  return (
    <AnimatePresence>
      {showHeader && (
        <motion.header
          initial={{ y: 0 }}
          animate={{ y: 0 }}
          exit={{ y: -100 }}
          transition={{ duration: 0.3, ease: "easeInOut" }}
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}
          onDrop={(e) => onDrop(e as DragEvent<HTMLDivElement>, true)}
          className={`sticky top-0 z-40 flex justify-center w-full transition-all duration-500 ease-out shadow-sm ${
            appState === "upload"
              ? "bg-transparent shadow-none"
              : "bg-[var(--color-bg-app)]/90 backdrop-blur-xl border-b border-[var(--color-border-subtle)] shadow-sm"
          } ${isDragging && appState !== "upload" ? "bg-[var(--color-info-soft)]/50" : ""}`}
        >
          <div
            className={`flex items-center justify-between w-[95%] xl:w-[90%] mx-auto px-6 ${appState === "upload" ? "py-4 md:py-6" : "py-3 md:py-4"} transition-all duration-300`}
          >
            <div
              className={`flex items-center gap-4 flex-shrink-0 ${appState !== "upload" ? "cursor-pointer hover:opacity-80 transition-opacity" : ""}`}
              onClick={() => {
                if (appState !== "upload") onNewFile();
              }}
              title={appState !== "upload" ? "Новый скриншот" : ""}
            >
              <AnimatePresence mode="popLayout" initial={false}>
                {appState === "upload" ? (
                  <motion.div
                    key="logo-view"
                    initial={{ opacity: 0, x: -20 }}
                    animate={{ opacity: 1, x: 0 }}
                    exit={{ opacity: 0, scale: 0.8 }}
                    transition={{ duration: 0.3 }}
                    className="flex items-center gap-3"
                  >
                    <div className="w-10 h-10 md:w-12 md:h-12 bg-accent rounded-xl flex items-center justify-center text-on-accent font-bold shadow-md text-lg">
                      TE
                    </div>
                    <div className="flex flex-col min-w-0 justify-center">
                      <div className="flex items-center gap-2">
                        <h1 className="font-bold tracking-tight text-[var(--color-text-primary)] leading-none truncate text-xl md:text-2xl">
                          Text Extractor
                        </h1>
                        <BugReportLink />
                      </div>
                      <span className="text-xs text-[var(--color-text-secondary)] font-medium tracking-wide mt-1 hidden sm:block">
                        Intelligent Text Migration
                      </span>
                    </div>
                  </motion.div>
                ) : (
                  <motion.div
                    key="file-view"
                    layoutId="file-upload-zone"
                    className="flex items-center gap-2.5 bg-[var(--color-bg-surface)] border border-[var(--color-border-default)] rounded-xl md:rounded-2xl p-1.5 shadow-sm pr-3 md:pr-4 group"
                    transition={{
                      type: "spring",
                      bounce: 0.2,
                      duration: 0.6,
                    }}
                  >
                    <div className="w-8 h-8 md:w-9 md:h-9 bg-[var(--color-info-soft)] rounded-lg md:rounded-xl flex items-center justify-center text-[var(--color-info-text)] relative overflow-hidden shrink-0">
                      <FileText className="w-4 h-4 md:w-4 md:h-4 group-hover:opacity-0 transition-opacity duration-300" />
                      <RefreshCw className="w-4 h-4 absolute inset-0 m-auto opacity-0 group-hover:opacity-100 transition-opacity duration-300" />
                    </div>
                    <div className="flex flex-col min-w-0 justify-center mt-0.5 md:mt-0">
                      <span className="text-[12px] md:text-[13px] font-bold text-[var(--color-text-primary)] truncate max-w-[120px] md:max-w-[200px] leading-tight group-hover:text-[var(--color-info-text)] transition-colors">
                        {file?.name}
                      </span>
                      <span className="text-[10px] text-[var(--color-text-secondary)] font-medium truncate leading-none mt-0.5">
                        Заменить файл
                      </span>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>

            <div className="flex items-center gap-2 ml-auto overflow-hidden">
              {appState !== "upload" && (
                <div className="hidden sm:flex items-center gap-1.5 overflow-x-auto no-scrollbar scroll-smooth">
                  <button
                    onClick={() => onSourceSelect("auto")}
                    className={btnClass("auto")}
                  >
                    Auto
                  </button>
                  <button
                    onClick={() => onSourceSelect("browser")}
                    className={`whitespace-nowrap ${btnClass("browser")}`}
                  >
                    Browser
                  </button>
                </div>
              )}
              <button
                onClick={onOpenSidebar}
                className={
                  appState === "upload"
                    ? "w-9 h-9 flex items-center justify-center text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)] hover:bg-[var(--color-bg-elevated)] rounded-xl transition-all shrink-0"
                    : "py-1.5 px-3 sm:px-4 ml-1 bg-[var(--color-bg-surface)] hover:bg-[var(--color-bg-elevated)] text-[var(--color-text-secondary)] rounded-xl transition-colors shadow-sm border border-[var(--color-border-default)] shrink-0 flex items-center justify-center gap-2 font-bold text-[13px] sm:text-sm"
                }
                title="Настройки"
              >
                {appState === "upload" ? (
                  <Settings className="w-5 h-5" />
                ) : (
                  <span className="truncate max-w-[100px] sm:max-w-[140px]">
                    {SOURCES.find((s) => s.id === selectedSource)?.label}
                  </span>
                )}
              </button>
            </div>
          </div>
        </motion.header>
      )}
    </AnimatePresence>
  );
}
