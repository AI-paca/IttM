import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, DragEvent, ReactNode } from "react";
import { useMotionValueEvent, useScroll } from "motion/react";
import { noticeFromError, requestApiJson } from "./api-client";
import { getBrowserDiagnostics, isSupportedOcrFile } from "./file-utils";
import { EXTERNAL_LLM_CONSENT_ERROR } from "./llm-consent";
import type {
  AppState,
  Notice,
  NoticeTone,
  ThemeLevel,
} from "../types/app.types";
import {
  AUTO_DARK,
  AUTO_LIGHT,
  applyPalette,
  interpolateWorkingScale,
} from "../ui/theme/palettes";
import type {
  AppDiagnostics,
  BackendDiagnostics,
  ExtractionDocumentProgress,
  LlmProvider,
  SourceType,
} from "./types";
import { useOcrExtraction } from "./use-extraction";
import {
  EngineControlsContext,
  NavigationAreaContext,
  OcrShellContext,
  OcrWorkspaceContext,
} from "./ocr-context";
import type {
  NavigationAreaContextValue,
  OcrShellContextValue,
  OcrWorkspaceContextValue,
} from "./ocr-context";
import type { EngineControls } from "../ui/layout/engine-controls.types";
import { SOURCES } from "../ui/sources";

function setCookie(k: string, v: string) {
  if (typeof document === "undefined") return;
  document.cookie = `${k}=${v}; path=/; max-age=31536000`;
}

function getCookie(k: string) {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(new RegExp("(^| )" + k + "=([^;]+)"));
  return match ? match[2] : null;
}

function delCookie(k: string) {
  if (typeof document === "undefined") return;
  document.cookie = `${k}=; path=/; max-age=0`;
}

function getSavedSource(): SourceType {
  const savedRemember = getCookie("text-extractor-remember");
  const savedSource = getCookie("text-extractor-source");
  if (
    savedRemember === "true" &&
    savedSource &&
    SOURCES.find((s) => s.id === savedSource)
  ) {
    return savedSource as SourceType;
  }
  return "auto";
}

function extensionFromMime(type: string): string {
  if (type === "image/jpeg") return "jpg";
  if (type === "image/webp") return "webp";
  if (type === "application/pdf") return "pdf";
  return "png";
}

function namedClipboardFile(file: File): File {
  if (file.name) return file;

  const extension = extensionFromMime(file.type);
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  return new File([file], `clipboard-${timestamp}.${extension}`, {
    type: file.type,
  });
}

function fileFromClipboard(event: ClipboardEvent): File | null {
  const clipboardData = event.clipboardData;
  if (!clipboardData) return null;

  for (const item of Array.from(clipboardData.items)) {
    if (item.kind !== "file") continue;

    const file = item.getAsFile();
    if (file && isSupportedOcrFile(file)) {
      return namedClipboardFile(file);
    }
  }

  for (const file of Array.from(clipboardData.files)) {
    if (isSupportedOcrFile(file)) {
      return namedClipboardFile(file);
    }
  }

  return null;
}

function getPreviewMode(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("preview");
}

function isLoadingPreviewMode(previewMode: string | null) {
  return previewMode === "loading" || previewMode === "transition";
}

const DEFAULT_PREVIEW_DOCUMENT_PROGRESS: ExtractionDocumentProgress = {
  currentPage: 3,
  totalPages: 6,
  completedPages: 2,
  currentPagePercent: 0.65,
  documentPercent: (2 + 0.65) / 6,
};

function previewTotalPages(params: URLSearchParams) {
  const requestedPages = Number(params.get("pages"));
  return Number.isFinite(requestedPages) && requestedPages > 0
    ? Math.floor(requestedPages)
    : (DEFAULT_PREVIEW_DOCUMENT_PROGRESS.totalPages ?? 6);
}

function getLoadingPreviewDocumentProgress(
  previewMode: string | null,
): ExtractionDocumentProgress | null {
  if (!isLoadingPreviewMode(previewMode)) return null;
  if (typeof window === "undefined") return DEFAULT_PREVIEW_DOCUMENT_PROGRESS;

  const params = new URLSearchParams(window.location.search);
  const totalPages = previewTotalPages(params);
  const progressParam = params.get("progress");
  if (progressParam === null) {
    if (totalPages === DEFAULT_PREVIEW_DOCUMENT_PROGRESS.totalPages) {
      return DEFAULT_PREVIEW_DOCUMENT_PROGRESS;
    }
    return {
      ...DEFAULT_PREVIEW_DOCUMENT_PROGRESS,
      totalPages,
      documentPercent:
        (DEFAULT_PREVIEW_DOCUMENT_PROGRESS.completedPages +
          DEFAULT_PREVIEW_DOCUMENT_PROGRESS.currentPagePercent) /
        totalPages,
    };
  }

  const requestedProgress = Number(progressParam);
  if (!Number.isFinite(requestedProgress)) {
    return DEFAULT_PREVIEW_DOCUMENT_PROGRESS;
  }

  const documentPercent = Math.max(0, Math.min(1, requestedProgress));
  const rawPageProgress = documentPercent * totalPages;
  const completedPages = Math.min(totalPages, Math.floor(rawPageProgress));
  const currentPage = Math.min(totalPages, completedPages + 1);
  const currentPagePercent =
    completedPages >= totalPages ? 1 : rawPageProgress - completedPages;

  return {
    currentPage,
    totalPages,
    completedPages,
    currentPagePercent,
    documentPercent,
  };
}

const PREVIEW_EXTRACTED_TEXT = `# Извлеченный текст

Тестовый документ на две страницы завершён. Этот preview нужен только для
визуальной проверки перехода от отмены к копированию.

| Страница | Состояние |
| --- | --- |
| 1 | распознана |
| 2 | распознана |`;

const PREVIEW_PARTIAL_TEXT = `# Извлеченный текст

Первая страница уже появилась, вторая ещё распознаётся. Это состояние проверяет
переход между страницами без резкой смены layout.

| Страница | Состояние |
| --- | --- |
| 1 | распознана |
| 2 | в обработке |`;

export function OcrProvider({ children }: { children: ReactNode }) {
  const previewMode = getPreviewMode();
  const previewDocumentProgress =
    getLoadingPreviewDocumentProgress(previewMode);
  const loadingPreview = previewDocumentProgress !== null;
  const [appState, setAppState] = useState<AppState>(
    loadingPreview ? "loading" : "upload",
  );
  const [file, setFile] = useState<File | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [selectedSource, setSelectedSource] = useState<SourceType>(() =>
    getSavedSource(),
  );
  const [activeSource, setActiveSource] = useState<SourceType | null>(null);
  const [pingUrl, setPingUrl] = useState("");
  const [rememberChoice, setRememberChoice] = useState(
    () => getCookie("text-extractor-remember") === "true",
  );
  const [showHeader, setShowHeader] = useState(true);
  const [copied, setCopied] = useState(false);
  const [extractedText, setExtractedText] = useState("");
  const [isExtracting, setIsExtracting] = useState(loadingPreview);
  const [extractionProgress, setExtractionProgress] = useState(
    loadingPreview ? "Отправка на сервер..." : "Достаём текст из скриншота...",
  );
  const [documentProgress, setDocumentProgress] =
    useState<ExtractionDocumentProgress | null>(
      loadingPreview ? previewDocumentProgress : null,
    );
  const [llmProvider, setLlmProvider] = useState<LlmProvider>("gemini");
  const [llmModel, setLlmModel] = useState("gemini-2.5-flash-lite");
  const [llmKey, setLlmKey] = useState("");
  const [externalLlmConsent, setExternalLlmConsent] = useState(false);
  const [themeLevel, setThemeLevel] = useState<ThemeLevel>(() => {
    if (typeof window !== "undefined") {
      const saved = Number(localStorage.getItem("theme-level"));
      return Number.isFinite(saved) && saved >= 0 && saved <= 1 ? saved : 0.5;
    }
    return 0.5;
  });
  const [themeAuto, setThemeAuto] = useState<boolean>(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem("theme-auto") !== "false";
    }
    return true;
  });
  const [easyOcrInstalling, setEasyOcrInstalling] = useState(false);
  const [easyOcrInstallMessage, setEasyOcrInstallMessage] = useState("");
  const [easyOcrInstallProgress, setEasyOcrInstallProgress] = useState(0);
  const [lastExtractedPage, setLastExtractedPage] = useState(1);
  const [totalPdfPages, setTotalPdfPages] = useState<number | null>(null);
  const [diagnostics, setDiagnostics] = useState<AppDiagnostics | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [triggerCount, setTriggerCount] = useState(0);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const { scrollY } = useScroll();
  const previewTransitionTotalPages =
    previewDocumentProgress?.totalPages ?? null;
  const previewTransitionInitialPercent =
    previewDocumentProgress?.documentPercent ?? 0;

  useEffect(() => {
    if (previewMode !== "transition" || previewTransitionTotalPages === null)
      return;
    const totalPages = previewTransitionTotalPages;
    const firstPagePercent =
      totalPages > 1
        ? Math.max(previewTransitionInitialPercent, (1 + 0.42) / totalPages)
        : 1;
    const firstPageTimeout = window.setTimeout(() => {
      setDocumentProgress({
        currentPage: Math.min(2, totalPages),
        totalPages,
        completedPages: Math.min(1, totalPages),
        currentPagePercent:
          totalPages > 1 ? Math.max(0, firstPagePercent * totalPages - 1) : 1,
        documentPercent: firstPagePercent,
      });
      setTotalPdfPages(totalPages);
      setLastExtractedPage(Math.min(2, totalPages));
      setExtractedText(PREVIEW_PARTIAL_TEXT);
      setAppState("reading");
    }, 650);

    const doneTimeout = window.setTimeout(() => {
      setDocumentProgress({
        currentPage: totalPages,
        totalPages,
        completedPages: totalPages,
        currentPagePercent: 1,
        documentPercent: 1,
      });
      setTotalPdfPages(totalPages);
      setLastExtractedPage(totalPages + 1);
      setExtractedText(PREVIEW_EXTRACTED_TEXT);
      setIsExtracting(false);
      setAppState("reading");
    }, 2100);

    return () => {
      window.clearTimeout(firstPageTimeout);
      window.clearTimeout(doneTimeout);
    };
  }, [
    previewMode,
    previewTransitionInitialPercent,
    previewTransitionTotalPages,
  ]);

  const showNotice = useCallback(
    (message: string, tone: NoticeTone = "error") => {
      setNotice({ message, tone });
      window.setTimeout(() => {
        setNotice((current) => (current?.message === message ? null : current));
      }, 6000);
    },
    [],
  );

  useEffect(() => {
    const browserInfo = getBrowserDiagnostics();

    requestApiJson<BackendDiagnostics>("/api/diagnostics", "Diagnostics")
      .then((data) => {
        setDiagnostics({ backend: data, browser: browserInfo });
      })
      .catch((error) => {
        const normalized = noticeFromError(error);
        setDiagnostics({
          backend: null,
          browser: browserInfo,
          error: normalized.message,
        });
      });
  }, []);

  // Текущее положение ползунка с учётом авто-режима:
  //  • Ручной режим: вся шкала WORKING_THEMES через рабочие IDE-чекпоинты
  //    (dark IDE -> muted workbench gray -> light IDE).
  //  • Авто-режим: напрямую повторяет системную тему браузера.
  const computePalette = useCallback(
    (auto: boolean, level: number, prefersDark: boolean) => {
      if (auto) {
        return prefersDark ? AUTO_DARK : AUTO_LIGHT;
      }
      return interpolateWorkingScale(level);
    },
    [],
  );

  useEffect(() => {
    localStorage.setItem("theme-level", String(themeLevel));
    localStorage.setItem("theme-auto", String(themeAuto));
  }, [themeLevel, themeAuto]);

  useEffect(() => {
    const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      const palette = computePalette(themeAuto, themeLevel, mediaQuery.matches);
      applyPalette(palette);
    };
    apply();

    if (!themeAuto) return;
    const handleChange = () => apply();
    mediaQuery.addEventListener("change", handleChange);
    return () => mediaQuery.removeEventListener("change", handleChange);
  }, [themeAuto, themeLevel, computePalette]);

  useMotionValueEvent(scrollY, "change", () => {
    setShowHeader(true);
  });

  const handleSourceSelect = useCallback(
    (src: SourceType) => {
      setActiveSource(null);
      setSelectedSource(src);
      if (rememberChoice) {
        setCookie("text-extractor-source", src);
      }
    },
    [rememberChoice],
  );

  const handleLlmProviderChange = useCallback((provider: LlmProvider) => {
    setLlmProvider(provider);
    setExternalLlmConsent(false);
  }, []);

  const handleRememberChange = useCallback(
    (checked: boolean) => {
      setRememberChoice(checked);
      if (checked) {
        setCookie("text-extractor-remember", "true");
        setCookie("text-extractor-source", selectedSource);
      } else {
        delCookie("text-extractor-remember");
        delCookie("text-extractor-source");
      }
    },
    [selectedSource],
  );

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(extractedText);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Failed to copy", err);
      showNotice("Не удалось скопировать текст в буфер обмена.");
    }
  }, [extractedText, showNotice]);

  const handleDragOver = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(true);
  }, []);

  const handleDragLeave = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
  }, []);

  const acceptFile = useCallback(
    (selected: File, autoStart: boolean = false) => {
      if (!isSupportedOcrFile(selected)) {
        showNotice("Поддерживаются только изображения и PDF.");
        return;
      }

      setFile(selected);
      setActiveSource(null);
      setDocumentProgress(null);
      if (autoStart) {
        setLastExtractedPage(1);
        setTotalPdfPages(null);
        setExtractedText("");
        setAppState("loading");
        setTriggerCount((prev) => prev + 1);
      } else {
        setAppState("configure");
      }
    },
    [showNotice],
  );

  const handleDrop = useCallback(
    (e: DragEvent<HTMLDivElement>, autoStart: boolean = false) => {
      e.preventDefault();
      setIsDragging(false);
      if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        acceptFile(e.dataTransfer.files[0], autoStart);
      }
    },
    [acceptFile],
  );

  const handleFileChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      if (e.target.files && e.target.files.length > 0) {
        acceptFile(e.target.files[0]);
      }
    },
    [acceptFile],
  );

  useEffect(() => {
    const handlePaste = (event: ClipboardEvent) => {
      const pastedFile = fileFromClipboard(event);
      if (!pastedFile) return;

      event.preventDefault();
      setIsDragging(false);
      acceptFile(pastedFile, true);
    };

    window.addEventListener("paste", handlePaste);
    return () => window.removeEventListener("paste", handlePaste);
  }, [acceptFile]);

  const handleNewFile = useCallback(() => {
    setAppState("upload");
    setFile(null);
    setActiveSource(null);
    setDocumentProgress(null);
  }, []);

  const handleStartExtraction = useCallback(() => {
    if (selectedSource === "llm" && !externalLlmConsent) {
      showNotice(EXTERNAL_LLM_CONSENT_ERROR);
      return;
    }
    setLastExtractedPage(1);
    setTotalPdfPages(null);
    setDocumentProgress(null);
    setExtractedText("");
    setActiveSource(null);
    setAppState("loading");
    setTriggerCount((prev) => prev + 1);
  }, [externalLlmConsent, selectedSource, showNotice]);

  const handleResumeExtraction = useCallback(() => {
    if (selectedSource === "llm" && !externalLlmConsent) {
      showNotice(EXTERNAL_LLM_CONSENT_ERROR);
      return;
    }
    setAppState("loading");
    setActiveSource(null);
    setTriggerCount((prev) => prev + 1);
  }, [externalLlmConsent, selectedSource, showNotice]);

  const handleInstallEasyOcr = useCallback(async () => {
    setEasyOcrInstalling(true);
    setEasyOcrInstallMessage("Запускаем установку EasyOCR...");
    setEasyOcrInstallProgress(3);

    type InstallStatus = {
      status?: string;
      phase?: string;
      message?: string;
      error?: string;
      progress?: number;
      logs?: string[];
    };

    try {
      await requestApiJson<InstallStatus>(
        "/api/install-easyocr",
        "EasyOCR install",
        {
          method: "POST",
        },
      );

      for (;;) {
        const status = await requestApiJson<InstallStatus>(
          "/api/install-easyocr/status",
          "EasyOCR install status",
        );
        const message =
          status.message || status.error || "Установка EasyOCR выполняется...";
        setEasyOcrInstallMessage(message);
        setEasyOcrInstallProgress(status.progress ?? 5);

        if (status.status === "installed") {
          showNotice(
            "EasyOCR установлен/найден. Можно использовать.",
            "success",
          );
          break;
        }

        if (status.status === "error") {
          showNotice(`EasyOCR: ${message}`);
          break;
        }

        await new Promise((resolve) => window.setTimeout(resolve, 1000));
      }
    } catch (error) {
      showNotice(noticeFromError(error).message);
    } finally {
      setEasyOcrInstalling(false);
    }
  }, [showNotice]);

  const { cancelExtraction } = useOcrExtraction({
    diagnostics,
    extractedText,
    externalLlmConsent,
    file,
    lastExtractedPage,
    llmKey,
    llmModel,
    llmProvider,
    pingUrl,
    selectedSource,
    triggerCount,
    setAppState,
    setExtractedText,
    setExtractionProgress,
    setDocumentProgress,
    setActiveSource,
    setIsExtracting,
    setLastExtractedPage,
    setTotalPdfPages,
    totalPdfPages,
    showNotice,
  });

  const closeNotice = useCallback(() => setNotice(null), []);

  const dragHandlers = useMemo(
    () => ({
      onDragOver: handleDragOver,
      onDragLeave: handleDragLeave,
      onDrop: handleDrop,
    }),
    [handleDragLeave, handleDragOver, handleDrop],
  );

  const engineControls = useMemo<EngineControls>(
    () => ({
      easyOcrInstallMessage,
      easyOcrInstallProgress,
      easyOcrInstalling,
      externalLlmConsent,
      llmKey,
      llmModel,
      llmProvider,
      pingUrl,
      rememberChoice,
      selectedSource,
      themeLevel,
      themeAuto,
      onInstallEasyOcr: handleInstallEasyOcr,
      onLlmProviderChange: handleLlmProviderChange,
      onRememberChange: handleRememberChange,
      onSourceSelect: handleSourceSelect,
      setLlmKey,
      setLlmModel,
      setExternalLlmConsent,
      setPingUrl,
      setThemeLevel,
      setThemeAuto,
    }),
    [
      easyOcrInstalling,
      easyOcrInstallMessage,
      easyOcrInstallProgress,
      externalLlmConsent,
      handleInstallEasyOcr,
      handleLlmProviderChange,
      handleRememberChange,
      handleSourceSelect,
      llmKey,
      llmModel,
      llmProvider,
      pingUrl,
      rememberChoice,
      selectedSource,
      themeLevel,
      themeAuto,
    ],
  );

  const shellValue = useMemo<OcrShellContextValue>(
    () => ({
      appState,
      dragHandlers,
      isDragging,
      notice,
      closeNotice,
    }),
    [appState, closeNotice, dragHandlers, isDragging, notice],
  );

  const navigationValue = useMemo<NavigationAreaContextValue>(
    () => ({
      activeSource,
      appState,
      dragHandlers,
      file,
      isDragging,
      isExtracting,
      showHeader,
      onNewFile: handleNewFile,
    }),
    [
      activeSource,
      appState,
      dragHandlers,
      file,
      handleNewFile,
      isDragging,
      isExtracting,
      showHeader,
    ],
  );

  const workspaceValue = useMemo<OcrWorkspaceContextValue>(
    () => ({
      appState,
      copied,
      diagnostics,
      dragHandlers,
      extractedText,
      documentProgress,
      extractionProgress,
      file,
      fileInputRef,
      isDragging,
      isExtracting,
      lastExtractedPage,
      totalPdfPages,
      onCancelExtraction: cancelExtraction,
      onCopy: handleCopy,
      onFileChange: handleFileChange,
      onNewFile: handleNewFile,
      onResumeExtraction: handleResumeExtraction,
      onStartExtraction: handleStartExtraction,
    }),
    [
      appState,
      cancelExtraction,
      copied,
      diagnostics,
      documentProgress,
      dragHandlers,
      extractedText,
      extractionProgress,
      file,
      handleCopy,
      handleFileChange,
      handleNewFile,
      handleResumeExtraction,
      handleStartExtraction,
      isDragging,
      isExtracting,
      lastExtractedPage,
      totalPdfPages,
    ],
  );

  return (
    <OcrShellContext.Provider value={shellValue}>
      <EngineControlsContext.Provider value={engineControls}>
        <NavigationAreaContext.Provider value={navigationValue}>
          <OcrWorkspaceContext.Provider value={workspaceValue}>
            {children}
          </OcrWorkspaceContext.Provider>
        </NavigationAreaContext.Provider>
      </EngineControlsContext.Provider>
    </OcrShellContext.Provider>
  );
}
