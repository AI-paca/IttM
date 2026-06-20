import type { ChangeEvent, DragEvent, RefObject } from "react";
import { UploadCloud } from "lucide-react";
import { motion } from "motion/react";
import type { AppDiagnostics } from "../ocr/types";
import { CropModeToggle } from "./upload/CropModeToggle";
import { DiagnosticsPanel } from "./upload/DiagnosticsPanel";

interface UploadPanelProps {
  diagnostics: AppDiagnostics | null;
  fileInputRef: RefObject<HTMLInputElement | null>;
  isDragging: boolean;
  onDragOver: (event: DragEvent<HTMLDivElement>) => void;
  onDragLeave: (event: DragEvent<HTMLDivElement>) => void;
  onDrop: (event: DragEvent<HTMLDivElement>, autoStart?: boolean) => void;
  onFileChange: (event: ChangeEvent<HTMLInputElement>) => void;
}

/**
 * Экран загрузки файла: drop-зона для изображений/PDF + панель диагностики.
 */
export function UploadPanel({
  diagnostics,
  fileInputRef,
  isDragging,
  onDragOver,
  onDragLeave,
  onDrop,
  onFileChange,
}: UploadPanelProps) {
  return (
    <motion.div
      key="upload-zone"
      layoutId="file-upload-zone"
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.95 }}
      className="w-full flex-1 flex flex-col gap-6 justify-center transition-all duration-300 relative z-0"
    >
      <div
        className={`w-full flex-1 min-h-[40vh] max-h-[60vh] rounded-[2.5rem] mt-4 md:mt-12 border-3 border-dashed relative flex flex-col items-center justify-center overflow-visible cursor-pointer transition-colors duration-200 ${
          isDragging
            ? "border-blue-500 bg-blue-50 dark:bg-blue-900/20"
            : "border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 hover:border-blue-400 hover:bg-gray-50 dark:hover:bg-gray-800"
        }`}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        onClick={() => fileInputRef.current?.click()}
      >
        <input
          type="file"
          ref={fileInputRef}
          className="hidden"
          accept="image/*,application/pdf"
          onChange={onFileChange}
        />
        <div className="flex flex-col items-center text-center p-6 pointer-events-none">
          <UploadCloud
            className={`w-16 h-16 mb-4 ${isDragging ? "text-blue-600" : "text-gray-400 dark:text-gray-500"}`}
          />
          <h2 className="text-xl md:text-2xl font-medium text-gray-800 dark:text-gray-100 mb-2">
            Перетащите документ или выберите файл
          </h2>
          <p className="text-gray-500 dark:text-gray-400 text-sm md:text-base">
            Поддерживаются изображения (PNG, JPG) и PDF документы
          </p>
        </div>

        <CropModeToggle />
      </div>

      {diagnostics && <DiagnosticsPanel diagnostics={diagnostics} />}
    </motion.div>
  );
}
