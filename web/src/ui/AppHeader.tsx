import type { DragEvent } from "react";
import { FileText, RefreshCw, Settings } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import type { AppState, SourceType } from "../ocr/types";
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
        ? "bg-blue-50 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 border-blue-300 dark:border-blue-700"
        : "bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-300 border-gray-200 dark:border-gray-700 hover:border-blue-300 dark:hover:border-gray-600"
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
          onDrop={(e) => onDrop(e, true)}
          className={`sticky top-0 z-40 flex justify-center w-full transition-all duration-500 ease-out shadow-sm ${
            appState === "upload"
              ? "bg-transparent shadow-none dark:bg-transparent"
              : "bg-white/90 dark:bg-gray-900/90 backdrop-blur-xl border-b border-gray-200/80 dark:border-gray-800 shadow-sm"
          } ${isDragging && appState !== "upload" ? "bg-blue-50/50 dark:bg-blue-900/20" : ""}`}
        >
          <div
            className={`flex items-center justify-between w-full max-w-7xl mx-auto px-4 ${appState === "upload" ? "py-4" : "py-3"} transition-colors duration-200`}
          >
            <div
              className={`flex items-center gap-3 flex-shrink-0 ${appState !== "upload" ? "cursor-pointer hover:opacity-80 transition-opacity" : ""}`}
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
                    <div className="w-9 h-9 md:w-10 md:h-10 bg-blue-600 rounded-xl flex items-center justify-center text-white font-bold shadow-sm">
                      TE
                    </div>
                    <div className="flex flex-col min-w-0 justify-center">
                      <h1 className="font-bold tracking-tight text-gray-900 dark:text-gray-100 leading-none truncate text-xl">
                        Text Extractor
                      </h1>
                    </div>
                  </motion.div>
                ) : (
                  <motion.div
                    key="file-view"
                    layoutId="file-upload-zone"
                    className="flex items-center gap-2.5 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700/50 rounded-xl md:rounded-2xl p-1.5 shadow-sm pr-3 md:pr-4 group"
                    transition={{
                      type: "spring",
                      bounce: 0.2,
                      duration: 0.6,
                    }}
                  >
                    <div className="w-8 h-8 md:w-9 md:h-9 bg-blue-50 dark:bg-blue-900/30 rounded-lg md:rounded-xl flex items-center justify-center text-blue-600 dark:text-blue-400 relative overflow-hidden shrink-0">
                      <FileText className="w-4 h-4 md:w-4 md:h-4 group-hover:opacity-0 transition-opacity duration-300" />
                      <RefreshCw className="w-4 h-4 absolute inset-0 m-auto opacity-0 group-hover:opacity-100 transition-opacity duration-300" />
                    </div>
                    <div className="flex flex-col min-w-0 justify-center mt-0.5 md:mt-0">
                      <span className="text-[12px] md:text-[13px] font-bold text-gray-900 dark:text-gray-100 truncate max-w-[120px] md:max-w-[200px] leading-tight group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors">
                        {file?.name}
                      </span>
                      <span className="text-[10px] text-gray-500 dark:text-gray-400 font-medium truncate leading-none mt-0.5">
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
                    ? "w-9 h-9 flex items-center justify-center text-gray-400 dark:text-gray-500 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-black/5 dark:hover:bg-white/5 rounded-xl transition-all shrink-0"
                    : "py-1.5 px-3 sm:px-4 ml-1 bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-200 rounded-xl transition-colors shadow-sm border border-gray-200 dark:border-gray-700 shrink-0 flex items-center justify-center gap-2 font-bold text-[13px] sm:text-sm"
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
