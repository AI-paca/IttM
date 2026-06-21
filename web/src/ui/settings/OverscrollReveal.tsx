import { X } from "lucide-react";

interface OverscrollRevealProps {
  isRevealed: boolean;
  overscroll: number;
  touchActive: boolean;
  onClose: () => void;
}

/**
 * Высота раскрытого блока со ссылкой на Issue Tracker.
 * Используется в нескольких местах (hook + сайдбар) — единый источник правды.
 */
export const REVEAL_HEIGHT = 120;

/**
 * Разворачиваемый блок внизу сайдбара с ссылками на Issue Tracker.
 * Появляется при pull-to-reveal жесте (overscroll внизу списка).
 *
 * Блок является нижним элементом потока внутри scroll-area. Основной контент
 * над ним не сжимается, поэтому reveal появляется в дополнительной области
 * после темы и закрывается обычным скроллом вверх от низа.
 */
export function OverscrollReveal({
  isRevealed,
  overscroll,
  touchActive,
  onClose,
}: OverscrollRevealProps) {
  const previewProgress = Math.min(overscroll / 60, 1);
  const panelOffset = isRevealed ? 0 : REVEAL_HEIGHT * (1 - previewProgress);

  return (
    <div
      className="w-full relative shrink-0 overflow-hidden"
      style={{
        height: isRevealed ? REVEAL_HEIGHT : overscroll,
        transition: touchActive ? "none" : "height 0.3s ease-out",
      }}
    >
      <div
        className="absolute bottom-0 left-0 w-full flex flex-col items-center justify-center border-t border-[var(--color-border-default)] bg-[var(--color-bg-surface)]"
        style={{
          height: REVEAL_HEIGHT,
          transform: `translateY(${panelOffset}px)`,
          transition: touchActive ? "none" : "transform 0.3s ease-out",
          pointerEvents: isRevealed ? "auto" : "none",
        }}
      >
        {isRevealed && (
          <button
            onClick={onClose}
            className="absolute top-2 right-2 z-10 p-1.5 text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)] bg-[var(--color-bg-elevated)] hover:bg-[var(--color-bg-inset)] rounded-full transition-colors border border-[var(--color-border-default)] shadow-sm"
            aria-label="Скрыть"
          >
            <X className="w-4 h-4" />
          </button>
        )}
        <div className="flex flex-col items-center text-center px-4 w-full h-full justify-center relative pointer-events-none">
          <span className="text-[12px] text-[var(--color-text-secondary)] font-bold mb-1 uppercase tracking-wider">
            ITTM Core
          </span>
          <a
            href="https://github.com/AI-paca/IttM/issues"
            target="_blank"
            rel="noreferrer"
            className="text-sm font-semibold text-[var(--color-info)] hover:text-[var(--color-info-text)] underline decoration-[var(--color-info-border)] transition-colors mb-2 cursor-pointer pointer-events-auto"
          >
            Issue Tracker / Support
          </a>
          <span className="text-[10px] text-[var(--color-text-muted)]">
            Environment: Preview
          </span>
        </div>
      </div>
    </div>
  );
}
