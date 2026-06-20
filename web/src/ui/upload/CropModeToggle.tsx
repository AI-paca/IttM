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
 * Выбор сохраняется в localStorage.
 *
 * Рефакторинг: gray классы заменены на .segmented-control / .segmented-btn.
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
      className="segmented-control absolute bottom-6 w-11/12 sm:w-1/2 lg:w-1/3 xl:w-1/4 z-10 pointer-events-auto"
      onClick={(e) => e.stopPropagation()}
    >
      {modes.map((mode) => (
        <button
          key={mode.id}
          onClick={() => handleCropChange(mode.id)}
          className={`segmented-btn text-[11px] sm:text-xs ${cropMode === mode.id ? "segmented-btn-active" : ""}`}
        >
          {mode.label}
        </button>
      ))}
    </div>
  );
}
