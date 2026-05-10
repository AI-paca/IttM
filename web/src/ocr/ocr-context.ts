import { createContext, useContext } from "react";
import type { ChangeEvent, DragEvent, RefObject } from "react";
import type { AppDiagnostics } from "./types";
import type { AppState, Notice } from "../types/app.types";
import type { EngineControls } from "../ui/layout/engine-controls.types";

export interface DragHandlers {
  onDragOver: (event: DragEvent<HTMLDivElement>) => void;
  onDragLeave: (event: DragEvent<HTMLDivElement>) => void;
  onDrop: (event: DragEvent<HTMLDivElement>, autoStart?: boolean) => void;
}

export interface OcrContextValue {
  appState: AppState;
  copied: boolean;
  diagnostics: AppDiagnostics | null;
  dragHandlers: DragHandlers;
  engineControls: EngineControls;
  extractedText: string;
  extractionProgress: string;
  file: File | null;
  fileInputRef: RefObject<HTMLInputElement | null>;
  isDragging: boolean;
  isExtracting: boolean;
  lastExtractedPage: number;
  notice: Notice | null;
  showHeader: boolean;
  totalPdfPages: number | null;
  closeNotice: () => void;
  onCancelExtraction: () => void;
  onCopy: () => Promise<void>;
  onFileChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onNewFile: () => void;
  onResumeExtraction: () => void;
  onStartExtraction: () => void;
}

export const OcrContext = createContext<OcrContextValue | null>(null);

export function useOcrApp() {
  const context = useContext(OcrContext);
  if (!context) {
    throw new Error("useOcrApp must be used inside OcrProvider");
  }
  return context;
}
