import { UploadCloud } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import type { AppState } from "../types/app.types";

interface DragOverlayProps {
  appState: AppState;
  isDragging: boolean;
}

export function DragOverlay({ appState, isDragging }: DragOverlayProps) {
  return (
    <AnimatePresence>
      {isDragging && appState !== "upload" && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-[100] flex items-center justify-center bg-[var(--color-info)]/10 backdrop-blur-md border-8 border-[var(--color-info)]/50 border-dashed m-4 rounded-[3rem] pointer-events-none"
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.9 }}
            transition={{ type: "spring", stiffness: 300, damping: 25 }}
            className="bg-[var(--color-bg-surface)] px-8 py-10 rounded-[2rem] shadow-2xl flex flex-col items-center gap-4 border-2 border-[var(--color-info)] scale-105"
          >
            <motion.div
              animate={{ scale: [1, 1.1, 1] }}
              transition={{
                repeat: Infinity,
                duration: 1.5,
                ease: "easeInOut",
              }}
              className="w-20 h-20 bg-[var(--color-info)] text-white rounded-full flex items-center justify-center shadow-lg"
            >
              <UploadCloud className="w-10 h-10" />
            </motion.div>
            <div className="text-center">
              <h3 className="text-2xl font-bold text-[var(--color-text-primary)]">
                Бросьте для замены
              </h3>
              <p className="text-[var(--color-text-secondary)] mt-1">
                Файл будет заменен и обработан сразу
              </p>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
