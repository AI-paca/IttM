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
        className="absolute bottom-0 left-0 w-full h-[120px] bg-gray-50 dark:bg-gray-950 flex flex-col items-center justify-center border-t border-gray-200 dark:border-gray-800"
        style={{
          opacity: isRevealed ? 1 : Math.min(overscroll / 60, 1),
          transition: touchActive ? "none" : "opacity 0.3s ease-out",
          pointerEvents: isRevealed ? "auto" : "none",
        }}
      >
        {isRevealed && (
          <button
            onClick={onClose}
            className="absolute top-2 right-2 p-1 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-200 dark:hover:bg-gray-800 rounded-full transition-colors"
            aria-label="Скрыть"
          >
            <X className="w-4 h-4" />
          </button>
        )}
        <div className="flex flex-col items-center text-center px-4 w-full h-full justify-center relative">
          <span className="text-[12px] text-gray-500 font-bold mb-1 uppercase tracking-wider">
            ITTM Core
          </span>
          <a
            href="https://github.com/AI-paca/IttM/issues"
            target="_blank"
            rel="noreferrer"
            className="text-sm font-semibold text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 underline decoration-blue-200 dark:decoration-blue-900 transition-colors mb-2 cursor-pointer pointer-events-auto"
          >
            Issue Tracker / Support
          </a>
          <span className="text-[10px] text-gray-400">
            Environment: Preview
          </span>
        </div>
      </div>
    </div>
  );
}
