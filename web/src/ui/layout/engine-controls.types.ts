import type { Dispatch, SetStateAction } from "react";
import type { ThemeMode } from "../../types/app.types";
import type { LlmProvider, SourceType } from "../../ocr/types";

export interface EngineControlsState {
  easyOcrInstallMessage: string;
  easyOcrInstallProgress: number;
  easyOcrInstalling: boolean;
  externalLlmConsent: boolean;
  llmKey: string;
  llmModel: string;
  llmProvider: LlmProvider;
  pingUrl: string;
  rememberChoice: boolean;
  selectedSource: SourceType;
  themeMode: ThemeMode;
}

export interface EngineControlsActions {
  onInstallEasyOcr: () => void;
  onLlmProviderChange: (provider: LlmProvider) => void;
  onRememberChange: (checked: boolean) => void;
  onSourceSelect: (source: SourceType) => void;
  setLlmKey: Dispatch<SetStateAction<string>>;
  setLlmModel: Dispatch<SetStateAction<string>>;
  setExternalLlmConsent: Dispatch<SetStateAction<boolean>>;
  setPingUrl: Dispatch<SetStateAction<string>>;
  setThemeMode: Dispatch<SetStateAction<ThemeMode>>;
}

export type EngineControls = EngineControlsState & EngineControlsActions;
