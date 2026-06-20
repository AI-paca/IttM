import { X } from "lucide-react";

interface OverscrollRevealProps {
  isRevealed: boolean;
  overscroll: number;
  touchActive: boolean;
  onClose: () => void;
}

/**
 * Разворачиваемый блок внизу сайдбара с ссылками на Issue Tracker.
 * Появляется при pull-to-reveal жесте (overscroll внизу списка).
 *
 * Блок является последним элементом в потоке скроллимого контейнера,
 * поэтому после раскрытия (scrollTop = scrollHeight) он оказывается
 * прижатым к низу видимой области.
 */
export function OverscrollReveal({
  isRevealed,
  overscroll,
  touchActive,
  onClose,
}: OverscrollRevealProps) {
  return (
    <div
      className="w-full relative shrink-0"
      style={{
        height: isRevealed ? 120 : 0,
        transition: !isRevealed ? "height 0.3s ease-out" : "none",
      }}
    >
      <div
        className="absolute bottom-0 left-0 w-full h-[120px] bg-[var(--color-bg-surface)] flex flex-col items-center justify-center border-t border-[var(--color-border-default)]"
        style={{
          opacity: isRevealed ? 1 : Math.min(overscroll / 60, 1),
          transition: touchActive ? "none" : "opacity 0.3s ease-out",
          pointerEvents: isRevealed ? "auto" : "none",
        }}
      >
        {isRevealed && (
          <button
            onClick={onClose}
            className="absolute top-2 right-2 p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)] hover:bg-[var(--color-bg-elevated)] rounded-full transition-colors"
            aria-label="Скрыть"
          >
            <X className="w-4 h-4" />
          </button>
        )}
        <div className="flex flex-col items-center text-center px-4 w-full h-full justify-center relative">
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
