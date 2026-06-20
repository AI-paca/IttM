import type { Dispatch, SetStateAction } from "react";
import type { ThemeLevel } from "../../types/app.types";
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
  themeLevel: ThemeLevel; // 0 = PURE_DARK, 1 = PURE_LIGHT
  themeAuto: boolean; // следовать за системной темой браузера
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
  setThemeLevel: Dispatch<SetStateAction<ThemeLevel>>;
  setThemeAuto: Dispatch<SetStateAction<boolean>>;
}

export type EngineControls = EngineControlsState & EngineControlsActions;
