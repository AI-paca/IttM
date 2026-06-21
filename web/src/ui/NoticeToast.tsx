import { X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import type { Notice } from "../types/app.types";

export type { Notice } from "../types/app.types";

interface NoticeToastProps {
  notice: Notice | null;
  onClose: () => void;
}

export function NoticeToast({ notice, onClose }: NoticeToastProps) {
  return (
    <AnimatePresence>
      {notice && (
        <motion.div
          initial={{ opacity: 0, y: -12 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -12 }}
          className={`fixed top-4 left-1/2 z-[120] w-[calc(100%-2rem)] max-w-xl -translate-x-1/2 rounded-2xl border px-4 py-3 shadow-xl backdrop-blur-xl ${
            notice.tone === "success"
              ? "border-[var(--color-success-border)] bg-[var(--color-success-soft)] text-[var(--color-success-text)]"
              : "border-[var(--color-danger-border)] bg-[var(--color-danger-soft)] text-[var(--color-danger-text)]"
          }`}
        >
          <div className="flex items-start gap-3">
            <div className="flex-1 text-sm font-semibold leading-5">
              {notice.message}
            </div>
            <button
              onClick={onClose}
              className="rounded-lg p-1 opacity-70 transition-opacity hover:opacity-100"
              title="Закрыть"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
