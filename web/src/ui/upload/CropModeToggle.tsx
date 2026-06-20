import { useState } from "react";

export type CropMode = "auto" | "manual" | "none";

const STORAGE_KEY = "ittm_crop_preference";

function readInitialMode(): CropMode {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "auto" || saved === "manual" || saved === "none") {
      return saved;
    }
  } catch {
    // ignore
  }
  return "auto";
}

/**
 * Переключатель режима обрезки изображения: «Как есть» / «Автообрезка» / «Вручную».
 * Выбор сохраняется в localStorage. Само действие пока заглушка (UI-only),
 * реальная обработка будет подключена позже.
 */
export function CropModeToggle() {
  const [cropMode, setCropMode] = useState<CropMode>(readInitialMode);

  const handleCropChange = (mode: CropMode) => {
    setCropMode(mode);
    try {
      localStorage.setItem(STORAGE_KEY, mode);
    } catch {
      // ignore
    }
  };

  const modes: { id: CropMode; label: string }[] = [
    { id: "none", label: "Как есть" },
    { id: "auto", label: "Автообрезка" },
    { id: "manual", label: "Вручную" },
  ];

  return (
    <div
      className="absolute bottom-6 flex bg-gray-100/80 dark:bg-gray-800/80 backdrop-blur-sm p-1 rounded-xl w-11/12 sm:w-1/2 lg:w-1/3 xl:w-1/4 z-10 pointer-events-auto border border-gray-200 dark:border-gray-700/50"
      onClick={(e) => e.stopPropagation()}
    >
      {modes.map((mode) => (
        <button
          key={mode.id}
          onClick={() => handleCropChange(mode.id)}
          className={`flex-1 py-1.5 text-[11px] sm:text-xs font-bold rounded-lg transition-all ${
            cropMode === mode.id
              ? "bg-white dark:bg-gray-700 shadow-sm text-gray-900 dark:text-white"
              : "text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300"
          }`}
        >
          {mode.label}
        </button>
      ))}
    </div>
  );
}
