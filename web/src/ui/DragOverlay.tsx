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
          className="fixed inset-0 z-[100] flex items-center justify-center bg-blue-600/10 backdrop-blur-md border-8 border-blue-500/50 border-dashed m-4 rounded-[3rem] pointer-events-none"
        >
          <div className="bg-white dark:bg-gray-900 px-8 py-10 rounded-[2rem] shadow-2xl flex flex-col items-center gap-4 border border-blue-200 dark:border-blue-800">
            <div className="w-20 h-20 bg-blue-600 text-white rounded-full flex items-center justify-center animate-pulse shadow-[0_0_20px_rgba(37,99,235,0.4)]">
              <UploadCloud className="w-10 h-10" />
            </div>
            <div className="text-center">
              <h3 className="text-2xl font-bold text-gray-900 dark:text-white">
                Бросьте для замены
              </h3>
              <p className="text-gray-500 dark:text-gray-400 mt-1">
                Файл будет заменен и обработан сразу
              </p>
            </div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
