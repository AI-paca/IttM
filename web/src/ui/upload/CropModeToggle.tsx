import { useState } from "react";
import {
  readCropMode,
  writeCropMode,
  type CropMode,
} from "../../lib/crop-preference";

/**
 * Переключатель режима обрезки изображения: «Как есть» / «Автообрезка» / «Вручную».
 * Выбор сохраняется в localStorage.
 *
 * Рефакторинг: gray классы заменены на .segmented-control / .segmented-btn.
 */
export function CropModeToggle() {
  const [cropMode, setCropMode] = useState<CropMode>(() => {
    const saved = readCropMode();
    return saved === "manual" ? "auto" : saved;
  });
  const [showManualStub, setShowManualStub] = useState(false);

  const handleCropChange = (mode: CropMode) => {
    if (mode === "manual") {
      setShowManualStub(true);
      window.setTimeout(() => setShowManualStub(false), 2600);
      return;
    }
    setShowManualStub(false);
    setCropMode(mode);
    writeCropMode(mode);
  };

  const modes: { id: CropMode; label: string }[] = [
    { id: "none", label: "Как есть" },
    { id: "auto", label: "Автообрезка" },
    { id: "manual", label: "Регион" },
  ];

  return (
    <div
      className="segmented-control absolute bottom-6 z-10 w-[min(92%,430px)] pointer-events-auto"
      onClick={(e) => e.stopPropagation()}
    >
      {showManualStub && (
        <div className="absolute bottom-[calc(100%+8px)] left-1/2 w-[min(92vw,360px)] -translate-x-1/2 rounded-lg border border-[var(--color-warning-border)] bg-[var(--color-warning-soft)] px-3 py-2 text-center text-[11px] font-semibold text-[var(--color-warning-text)] shadow-sm">
          Ручной регион пока в очереди. Сейчас применяется автообрезка.
        </div>
      )}
      {modes.map((mode) => (
        <button
          key={mode.id}
          type="button"
          onClick={() => handleCropChange(mode.id)}
          className={`segmented-btn min-h-10 text-[11px] sm:min-h-0 sm:text-xs ${
            cropMode === mode.id ? "segmented-btn-active" : ""
          } ${mode.id === "manual" ? "opacity-65" : ""}`}
          title={
            mode.id === "manual"
              ? "Ручной регион пока не реализован"
              : undefined
          }
        >
          {mode.label}
        </button>
      ))}
    </div>
  );
}
