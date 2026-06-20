import { Check, DownloadCloud } from "lucide-react";
import type { SourceType } from "../../ocr/types";
import type { SourceOption } from "../sources";
import { StickerBg } from "../StickerBg";
import { getSourceBadge } from "./source-badges";

interface SourceCardProps {
  src: SourceOption;
  isMobile: boolean;
  isActive: boolean;
  isPeeled: boolean;
  easyOcrInstalling: boolean;
  easyOcrInstallMessage: string;
  easyOcrInstallProgress: number;
  onSelect: (id: SourceType) => void;
  onInstallEasyOcr: () => void;
  onTogglePeel: (id: string) => void;
}

/**
 * Карточка источника OCR: 3D-стикер с эффектом "отклеивания".
 * При отклеивании показывает бейдж безопасности.
 */
export function SourceCard({
  src,
  isMobile,
  isActive,
  isPeeled,
  easyOcrInstalling,
  easyOcrInstallMessage,
  easyOcrInstallProgress,
  onSelect,
  onInstallEasyOcr,
  onTogglePeel,
}: SourceCardProps) {
  const disabled = isMobile && ["local_tess", "local_easy"].includes(src.id);
  const badge = getSourceBadge(src.id);

  return (
    <div
      className={`group relative w-full text-left rounded-xl border-0 overflow-visible ${
        disabled ? "opacity-40 grayscale" : ""
      } h-[64px] sm:h-[76px]`}
    >
      {/* Layer 1: Background (Safety info exposed when peeled) */}
      {badge && (
        <div
          className={`absolute inset-0 ${badge.bgDark} rounded-xl flex flex-col items-start justify-center pt-1 pl-4 pr-12 cursor-pointer z-0 pointer-events-auto`}
          onClick={() => onTogglePeel("")}
        >
          <span
            className={`text-[11px] font-bold uppercase tracking-wide opacity-90 ${badge.isNeutral ? "text-gray-700 dark:text-gray-200" : "text-white"}`}
          >
            {badge.text}
          </span>
          <span
            className={`text-[10px] text-left leading-snug mt-0.5 line-clamp-3 whitespace-pre-wrap ${badge.isNeutral ? "text-gray-600 dark:text-gray-400" : "text-white/90"}`}
          >
            {badge.desc}
          </span>
        </div>
      )}

      <StickerBg
        peeled={isPeeled}
        active={isActive}
        r={12}
        baseDx={54}
        baseDy={22}
        curve={0.15}
        shadow={0.33}
        animMode="peel"
      >
        <div
          onClick={() => {
            if (isPeeled) {
              onTogglePeel("");
              return;
            }
            if (!disabled) onSelect(src.id as SourceType);
          }}
          className={`absolute inset-0 flex items-center justify-between pl-3 pr-[40px] z-10 cursor-pointer ${
            isPeeled ? "pointer-events-none" : "pointer-events-auto"
          }`}
        >
          <div className="flex items-center gap-3 w-full">
            <div
              className={
                isActive
                  ? "text-blue-600 dark:text-blue-400"
                  : "text-gray-400 dark:text-gray-500"
              }
            >
              {src.icon}
            </div>
            <div className="flex flex-col flex-1 min-w-0">
              <span
                className={`text-[13px] font-semibold truncate ${isActive ? "text-blue-700 dark:text-blue-300" : "text-gray-700 dark:text-gray-200"}`}
              >
                {src.label} {disabled ? "(Не для моб.)" : ""}
              </span>
              <span className="text-[11px] text-gray-500 dark:text-gray-400 leading-tight mt-0.5 truncate">
                {src.desc}
              </span>
            </div>
          </div>
        </div>

        <div className="absolute right-0 top-0 bottom-0 pr-10 flex flex-row items-center gap-2 z-20 pointer-events-none">
          {src.id === "local_easy" && !easyOcrInstalling && isActive && (
            <div
              onClick={(e) => {
                e.stopPropagation();
                onInstallEasyOcr();
              }}
              className="p-1 min-w-[28px] h-[28px] flex justify-center items-center pointer-events-auto cursor-pointer bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg text-gray-400 hover:text-blue-500 hover:border-blue-300 dark:hover:border-blue-700 shadow-sm active:scale-95 transition-all text-center"
              title="Скачать EasyOCR (~5ГБ)"
            >
              <DownloadCloud className="w-4 h-4" />
            </div>
          )}
          {src.id === "local_easy" && easyOcrInstalling && (
            <div
              className="w-[84px] px-2 py-1 bg-blue-50 dark:bg-blue-900 border border-blue-200 dark:border-blue-700 rounded text-[9px] text-blue-700 dark:text-blue-200 pointer-events-auto"
              title={easyOcrInstallMessage}
            >
              <div className="flex items-center justify-between gap-1">
                <span className="truncate">
                  {Math.round(easyOcrInstallProgress)}%
                </span>
                <div className="w-2 h-2 border-[1.5px] border-blue-500 border-t-transparent rounded-full animate-spin shrink-0" />
              </div>
              <div className="mt-1 h-[2px] overflow-hidden rounded-full bg-blue-100 dark:bg-blue-950">
                <div
                  className="h-full rounded-full bg-blue-500 transition-all duration-500"
                  style={{
                    width: `${Math.max(3, Math.min(100, easyOcrInstallProgress))}%`,
                  }}
                />
              </div>
            </div>
          )}
          {isActive && (
            <Check className="w-4 h-4 text-blue-600 dark:text-blue-400 shrink-0" />
          )}
        </div>
      </StickerBg>

      <div
        onClick={(e) => {
          e.stopPropagation();
          onTogglePeel(isPeeled ? "" : src.id);
        }}
        className="absolute top-0 right-0 w-12 h-12 z-30 cursor-pointer pointer-events-auto"
        title="Show details"
      />
    </div>
  );
}
