export type CropMode = "auto" | "manual" | "none";
export type EffectivePdfCropMode = "auto" | "none";

const STORAGE_KEY = "ittm_crop_preference";

export function readCropMode(): CropMode {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "auto" || saved === "manual" || saved === "none") {
      return saved;
    }
  } catch {
    // localStorage may be unavailable in tests or private browser contexts.
  }
  return "auto";
}

export function writeCropMode(mode: CropMode): void {
  try {
    localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // Preference persistence is best-effort only.
  }
}

export function effectivePdfCropMode(mode: CropMode): EffectivePdfCropMode {
  return mode === "none" ? "none" : "auto";
}
