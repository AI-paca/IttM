import type { ChangeEvent, DragEvent, RefObject } from "react";
import { Activity, Cpu, UploadCloud } from "lucide-react";
import { motion } from "motion/react";
import type { AppDiagnostics, BackendGpuInfo } from "../ocr/types";

interface UploadPanelProps {
  diagnostics: AppDiagnostics | null;
  fileInputRef: RefObject<HTMLInputElement | null>;
  isDragging: boolean;
  onDragOver: (event: DragEvent<HTMLDivElement>) => void;
  onDragLeave: (event: DragEvent<HTMLDivElement>) => void;
  onDrop: (event: DragEvent<HTMLDivElement>, autoStart?: boolean) => void;
  onFileChange: (event: ChangeEvent<HTMLInputElement>) => void;
}

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
      className="w-full relative flex flex-col gap-6"
    >
      <div
        className={`w-full min-h-[300px] md:min-h-[400px] rounded-[2.5rem] mt-4 md:mt-12 border-3 border-dashed relative flex flex-col items-center justify-center overflow-hidden cursor-pointer transition-colors duration-200 ${
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
      </div>

      {diagnostics && (
        <div className="bg-white dark:bg-gray-800 rounded-2xl border border-gray-200 dark:border-gray-700 p-4 shadow-sm flex flex-col gap-3 transition-colors delay-100">
          <h3 className="text-sm font-bold text-gray-700 dark:text-gray-300 flex items-center gap-2">
            <Activity className="w-4 h-4 text-blue-500" /> Diagnostics & Limits
          </h3>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs text-gray-600 dark:text-gray-400">
            <div className="flex flex-col bg-gray-50 dark:bg-gray-900 p-2.5 rounded-lg border border-gray-100 dark:border-gray-700/50">
              <span className="font-semibold text-gray-800 dark:text-gray-200 mb-0.5">
                Local Memory
              </span>
              {diagnostics.browser.memory} GB
            </div>
            <div className="flex flex-col bg-gray-50 dark:bg-gray-900 p-2.5 rounded-lg border border-gray-100 dark:border-gray-700/50">
              <span className="font-semibold text-gray-800 dark:text-gray-200 mb-0.5">
                Local CPU
              </span>
              {diagnostics.browser.cores} Cores
            </div>
            {diagnostics.backend ? (
              <>
                <div className="flex flex-col bg-blue-50 dark:bg-blue-900/20 p-2.5 rounded-lg border border-blue-100 dark:border-blue-900/50">
                  <span className="font-semibold text-blue-800 dark:text-blue-200 mb-0.5">
                    Backend RAM
                  </span>
                  {diagnostics.backend.memory_used_gb} /{" "}
                  {diagnostics.backend.memory_total_gb} GB
                </div>
                <div className="flex flex-col bg-blue-50 dark:bg-blue-900/20 p-2.5 rounded-lg border border-blue-100 dark:border-blue-900/50">
                  <span className="font-semibold text-blue-800 dark:text-blue-200 mb-0.5">
                    Backend System
                  </span>
                  {diagnostics.backend.system} / {diagnostics.backend.cpu_cores}{" "}
                  Cores
                </div>
                {diagnostics.backend.gpus &&
                diagnostics.backend.gpus.length > 0 ? (
                  <div className="col-span-2 sm:col-span-4 flex gap-2 flex-wrap mt-1">
                    {diagnostics.backend.gpus.map(
                      (g: BackendGpuInfo, i: number) => (
                        <div
                          key={i}
                          className="flex items-center gap-1.5 px-3 py-1.5 bg-indigo-50 dark:bg-indigo-900/20 rounded-lg border border-indigo-100 dark:border-indigo-900/50 font-medium text-xs text-indigo-700 dark:text-indigo-300"
                        >
                          <Cpu className="w-3.5 h-3.5" />
                          {g.name} {g.version && `(v${g.version})`}
                        </div>
                      ),
                    )}
                  </div>
                ) : (
                  <div className="col-span-2 sm:col-span-4 flex gap-2 flex-wrap mt-1">
                    <div
                      className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg border font-medium text-xs ${
                        diagnostics.backend.gpu_error
                          ? "bg-amber-50 dark:bg-amber-900/20 border-amber-200 dark:border-amber-800 text-amber-600 dark:text-amber-400"
                          : "bg-gray-50 dark:bg-gray-900/20 border-gray-200 dark:border-gray-800 text-gray-500 dark:text-gray-400"
                      }`}
                    >
                      <Cpu className="w-3.5 h-3.5" />
                      {diagnostics.backend.gpu_error
                        ? `GPU Error: ${diagnostics.backend.gpu_error}`
                        : "No GPU Detected (CPU Mode)"}
                    </div>
                  </div>
                )}
              </>
            ) : (
              <div className="col-span-2 flex items-center bg-red-50 dark:bg-red-900/10 text-red-500 p-2.5 rounded-lg border border-red-100 dark:border-red-900/50 font-medium">
                Backend offline
              </div>
            )}
          </div>
        </div>
      )}
    </motion.div>
  );
}
