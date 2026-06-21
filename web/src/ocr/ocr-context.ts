import { createContext, useContext } from "react";
import type { ChangeEvent, Context, DragEvent, RefObject } from "react";
import type { AppDiagnostics, SourceType } from "./types";
import type { AppState, Notice } from "../types/app.types";
import type { EngineControls } from "../ui/layout/engine-controls.types";

export interface DragHandlers {
  onDragOver: (event: DragEvent<HTMLDivElement>) => void;
  onDragLeave: (event: DragEvent<HTMLDivElement>) => void;
  onDrop: (event: DragEvent<HTMLDivElement>, autoStart?: boolean) => void;
}

export interface OcrShellContextValue {
  appState: AppState;
  dragHandlers: DragHandlers;
  isDragging: boolean;
  notice: Notice | null;
  closeNotice: () => void;
}

export interface NavigationAreaContextValue {
  activeSource: SourceType | null;
  appState: AppState;
  dragHandlers: DragHandlers;
  file: File | null;
  isDragging: boolean;
  showHeader: boolean;
  onNewFile: () => void;
}

export interface OcrWorkspaceContextValue {
  appState: AppState;
  copied: boolean;
  diagnostics: AppDiagnostics | null;
  dragHandlers: DragHandlers;
  extractedText: string;
  extractionProgress: string;
  file: File | null;
  fileInputRef: RefObject<HTMLInputElement | null>;
  isDragging: boolean;
  isExtracting: boolean;
  lastExtractedPage: number;
  totalPdfPages: number | null;
  onCancelExtraction: () => void;
  onCopy: () => Promise<void>;
  onFileChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onNewFile: () => void;
  onResumeExtraction: () => void;
  onStartExtraction: () => void;
}

export const OcrShellContext = createContext<OcrShellContextValue | null>(null);
export const NavigationAreaContext =
  createContext<NavigationAreaContextValue | null>(null);
export const EngineControlsContext = createContext<EngineControls | null>(null);
export const OcrWorkspaceContext =
  createContext<OcrWorkspaceContextValue | null>(null);

function useRequiredContext<T>(context: Context<T | null>, hookName: string) {
  const value = useContext(context);
  if (!value) {
    throw new Error(`${hookName} must be used inside OcrProvider`);
  }
  return value;
}

export function useOcrShell() {
  return useRequiredContext(OcrShellContext, "useOcrShell");
}

export function useNavigationArea() {
  return useRequiredContext(NavigationAreaContext, "useNavigationArea");
}

export function useEngineControls() {
  return useRequiredContext(EngineControlsContext, "useEngineControls");
}

export function useOcrWorkspace() {
  return useRequiredContext(OcrWorkspaceContext, "useOcrWorkspace");
}
