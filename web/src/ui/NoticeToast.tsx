import { X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";

export interface Notice {
  message: string;
  tone: "error" | "success";
}

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
              ? "border-green-200 bg-green-50/95 text-green-800 dark:border-green-800 dark:bg-green-900/90 dark:text-green-100"
              : "border-red-200 bg-red-50/95 text-red-800 dark:border-red-800 dark:bg-red-950/90 dark:text-red-100"
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
