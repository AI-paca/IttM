import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, DragEvent, ReactNode } from "react";
import { useMotionValueEvent, useScroll } from "motion/react";
import { noticeFromError, requestApiJson } from "./api-client";
import { getBrowserDiagnostics, isSupportedOcrFile } from "./file-utils";
import type {
  AppState,
  Notice,
  NoticeTone,
  ThemeMode,
} from "../types/app.types";
import type {
  AppDiagnostics,
  BackendDiagnostics,
  LlmProvider,
  SourceType,
} from "./types";
import { useOcrExtraction } from "./use-extraction";
import { OcrContext } from "./ocr-context";
import type { OcrContextValue } from "./ocr-context";
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

export function OcrProvider({ children }: { children: ReactNode }) {
  const [appState, setAppState] = useState<AppState>("upload");
  const [file, setFile] = useState<File | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [selectedSource, setSelectedSource] = useState<SourceType>(() =>
    getSavedSource(),
  );
  const [pingUrl, setPingUrl] = useState("");
  const [rememberChoice, setRememberChoice] = useState(
    () => getCookie("text-extractor-remember") === "true",
  );
  const [showHeader, setShowHeader] = useState(true);
  const [copied, setCopied] = useState(false);
  const [extractedText, setExtractedText] = useState("");
  const [isExtracting, setIsExtracting] = useState(false);
  const [extractionProgress, setExtractionProgress] = useState(
    "Достаём текст из скриншота...",
  );
  const [llmProvider, setLlmProvider] = useState<LlmProvider>("gemini");
  const [llmModel, setLlmModel] = useState("gemini-2.5-flash-lite");
  const [llmKey, setLlmKey] = useState("");
  const [themeMode, setThemeMode] = useState<ThemeMode>(() => {
    if (typeof window !== "undefined") {
      const saved = localStorage.getItem("theme-mode");
      return saved === "light" || saved === "dark" || saved === "auto"
        ? saved
        : "auto";
    }
    return "auto";
  });
  const [easyOcrInstalling, setEasyOcrInstalling] = useState(false);
  const [lastExtractedPage, setLastExtractedPage] = useState(1);
  const [totalPdfPages, setTotalPdfPages] = useState<number | null>(null);
  const [diagnostics, setDiagnostics] = useState<AppDiagnostics | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [triggerCount, setTriggerCount] = useState(0);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const { scrollY } = useScroll();

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

  useEffect(() => {
    localStorage.setItem("theme-mode", themeMode);
    const applyDark = () => document.documentElement.classList.add("dark");
    const applyLight = () => document.documentElement.classList.remove("dark");

    if (themeMode === "dark") applyDark();
    else if (themeMode === "light") applyLight();
    else if (window.matchMedia("(prefers-color-scheme: dark)").matches)
      applyDark();
    else applyLight();
  }, [themeMode]);

  useEffect(() => {
    if (themeMode !== "auto") return;
    const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
    const handleChange = (e: MediaQueryListEvent) => {
      if (e.matches) document.documentElement.classList.add("dark");
      else document.documentElement.classList.remove("dark");
    };
    mediaQuery.addEventListener("change", handleChange);
    return () => mediaQuery.removeEventListener("change", handleChange);
  }, [themeMode]);

  useMotionValueEvent(scrollY, "change", () => {
    setShowHeader(true);
  });

  const handleSourceSelect = useCallback(
    (src: SourceType) => {
      setSelectedSource(src);
      if (rememberChoice) {
        setCookie("text-extractor-source", src);
      }
    },
    [rememberChoice],
  );

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
  }, []);

  const handleStartExtraction = useCallback(() => {
    setLastExtractedPage(1);
    setTotalPdfPages(null);
    setExtractedText("");
    setAppState("loading");
    setTriggerCount((prev) => prev + 1);
  }, []);

  const handleResumeExtraction = useCallback(() => {
    setAppState("loading");
    setTriggerCount((prev) => prev + 1);
  }, []);

  const handleInstallEasyOcr = useCallback(() => {
    setEasyOcrInstalling(true);
    requestApiJson<{
      status?: string;
      message?: string;
      error?: string;
    }>("/api/install-easyocr", "EasyOCR install", {
      method: "POST",
    })
      .then((d) => {
        if (d.status === "already_installed" || d.status === "installed") {
          showNotice(
            "EasyOCR установлен/найден. Можно использовать.",
            "success",
          );
        } else {
          showNotice(
            `EasyOCR: ${d.message || d.error || "не удалось установить"}`,
          );
        }
      })
      .catch((error) => {
        showNotice(noticeFromError(error).message);
      })
      .finally(() => setEasyOcrInstalling(false));
  }, [showNotice]);

  const { cancelExtraction } = useOcrExtraction({
    appState,
    diagnostics,
    extractedText,
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
    setIsExtracting,
    setLastExtractedPage,
    setTotalPdfPages,
    showNotice,
  });

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
      easyOcrInstalling,
      llmKey,
      llmModel,
      llmProvider,
      pingUrl,
      rememberChoice,
      selectedSource,
      themeMode,
      onInstallEasyOcr: handleInstallEasyOcr,
      onRememberChange: handleRememberChange,
      onSourceSelect: handleSourceSelect,
      setLlmKey,
      setLlmModel,
      setLlmProvider,
      setPingUrl,
      setThemeMode,
    }),
    [
      easyOcrInstalling,
      handleInstallEasyOcr,
      handleRememberChange,
      handleSourceSelect,
      llmKey,
      llmModel,
      llmProvider,
      pingUrl,
      rememberChoice,
      selectedSource,
      themeMode,
    ],
  );

  const value = useMemo<OcrContextValue>(
    () => ({
      appState,
      copied,
      diagnostics,
      dragHandlers,
      engineControls,
      extractedText,
      extractionProgress,
      file,
      fileInputRef,
      isDragging,
      isExtracting,
      lastExtractedPage,
      notice,
      showHeader,
      totalPdfPages,
      closeNotice: () => setNotice(null),
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
      dragHandlers,
      engineControls,
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
      notice,
      showHeader,
      totalPdfPages,
    ],
  );

  return <OcrContext.Provider value={value}>{children}</OcrContext.Provider>;
}
