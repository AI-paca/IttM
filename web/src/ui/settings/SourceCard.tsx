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
 * При отклеивании показывает бейдж безопасности (цвет из tokens.css).
 *
 * Рефакторинг: цвета переведены на семантические токены (text-secondary,
 * text-muted, accent, surface) — хардкодные gray/blue классы удалены.
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
  const selectSource = () => {
    if (disabled) return;
    onSelect(src.id as SourceType);
    if (isPeeled) onTogglePeel("");
  };

  return (
    <div
      className={`group relative w-full text-left rounded-xl border-0 overflow-visible ${
        disabled ? "opacity-40 grayscale" : ""
      } h-[72px] sm:h-[76px]`}
    >
      {/* Слой 1: фон (инфо о безопасности, виден при отклеивании) */}
      {badge && (
        <div
          className={`absolute inset-0 rounded-xl flex flex-col items-start justify-center pt-1 pl-4 pr-12 z-0 pointer-events-auto ${
            disabled ? "cursor-not-allowed" : "cursor-pointer"
          }`}
          style={{ background: badge.bgVar }}
          onClick={selectSource}
        >
          <span
            className="text-[11px] font-bold uppercase tracking-wide opacity-90"
            style={{ color: badge.textVar }}
          >
            {badge.text}
          </span>
          <span
            className="text-[10px] text-left leading-snug mt-0.5 line-clamp-3 whitespace-pre-wrap opacity-90"
            style={{ color: badge.descVar }}
          >
            {badge.desc}
          </span>
        </div>
      )}

      <StickerBg
        peeled={isPeeled}
        active={isActive}
        className={isPeeled ? "pointer-events-none" : undefined}
        r={12}
        baseDx={54}
        baseDy={22}
        curve={0.15}
        shadow={0.33}
        animMode="peel"
        initialSize={{ w: 320, h: isMobile ? 72 : 76 }}
        oversizePct={0.02}
      >
        <div
          onClick={selectSource}
          className={`absolute inset-0 flex items-center justify-between pl-3 pr-[40px] z-10 ${
            disabled ? "cursor-not-allowed" : "cursor-pointer"
          } ${isPeeled ? "pointer-events-none" : "pointer-events-auto"}`}
        >
          <div className="flex items-center gap-3 w-full">
            <div className={isActive ? "text-accent" : "text-faint"}>
              {src.icon}
            </div>
            <div className="flex flex-col flex-1 min-w-0">
              <span
                className={`text-[14px] font-semibold truncate sm:text-[13px] ${
                  isActive ? "text-accent-strong" : "text-secondary"
                }`}
              >
                {src.label} {disabled ? "(Не для моб.)" : ""}
              </span>
              <span className="text-[11.5px] text-muted leading-tight mt-0.5 truncate sm:text-[11px]">
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
              className="p-1 min-w-[28px] h-[28px] flex justify-center items-center pointer-events-auto cursor-pointer bg-surface border border-default rounded-lg text-faint hover:text-accent hover:border-accent-soft-border shadow-sm active:scale-95 transition-all text-center"
              title="Скачать EasyOCR (~5ГБ)"
            >
              <DownloadCloud className="w-4 h-4" />
            </div>
          )}
          {src.id === "local_easy" && easyOcrInstalling && (
            <div
              className="w-[84px] px-2 py-1 bg-accent-soft border border-accent-soft-border rounded text-[9px] text-accent-strong pointer-events-auto"
              title={easyOcrInstallMessage}
            >
              <div className="flex items-center justify-between gap-1">
                <span className="truncate">
                  {Math.round(easyOcrInstallProgress)}%
                </span>
                <div className="w-2 h-2 border-[1.5px] border-accent border-t-transparent rounded-full animate-spin shrink-0" />
              </div>
              <div className="mt-1 h-[2px] overflow-hidden rounded-full bg-accent-soft">
                <div
                  className="h-full rounded-full bg-accent transition-all duration-500"
                  style={{
                    width: `${Math.max(3, Math.min(100, easyOcrInstallProgress))}%`,
                  }}
                />
              </div>
            </div>
          )}
          {isActive && <Check className="w-4 h-4 text-accent shrink-0" />}
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
