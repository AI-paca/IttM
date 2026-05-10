import { useEffect, useRef, useState } from "react";
import type { ChangeEvent, DragEvent } from "react";
import { AnimatePresence, useMotionValueEvent, useScroll } from "motion/react";
import { noticeFromError, requestApiJson } from "./ocr/api-client";
import { getBrowserDiagnostics, isSupportedOcrFile } from "./ocr/file-utils";
import type {
  AppDiagnostics,
  AppState,
  BackendDiagnostics,
  LlmProvider,
  SourceType,
} from "./ocr/types";
import { useOcrExtraction } from "./ocr/use-extraction";
import { AppHeader } from "./ui/AppHeader";
import { ConfigurePanel } from "./ui/ConfigurePanel";
import { DragOverlay } from "./ui/DragOverlay";
import { LoadingPanel } from "./ui/LoadingPanel";
import { NoticeToast } from "./ui/NoticeToast";
import type { Notice } from "./ui/NoticeToast";
import { ReadingPanel } from "./ui/ReadingPanel";
import { SettingsSidebar } from "./ui/SettingsSidebar";
import { SOURCES } from "./ui/sources";
import { UploadPanel } from "./ui/UploadPanel";

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

export default function App() {
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
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [extractedText, setExtractedText] = useState("");
  const [isExtracting, setIsExtracting] = useState(false);
  const [extractionProgress, setExtractionProgress] = useState(
    "Достаём текст из скриншота...",
  );
  const [llmProvider, setLlmProvider] = useState<LlmProvider>("gemini");
  const [llmModel, setLlmModel] = useState("gemini-2.5-flash-lite");
  const [llmKey, setLlmKey] = useState("");
  const [themeMode, setThemeMode] = useState<"light" | "dark" | "auto">(() => {
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

  const showNotice = (message: string, tone: "error" | "success" = "error") => {
    setNotice({ message, tone });
    window.setTimeout(() => {
      setNotice((current) => (current?.message === message ? null : current));
    }, 6000);
  };

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
    else {
      if (window.matchMedia("(prefers-color-scheme: dark)").matches)
        applyDark();
      else applyLight();
    }
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

  const handleSourceSelect = (src: SourceType) => {
    setSelectedSource(src);
    if (rememberChoice) {
      setCookie("text-extractor-source", src);
    }
  };

  const handleRememberChange = (checked: boolean) => {
    setRememberChoice(checked);
    if (checked) {
      setCookie("text-extractor-remember", "true");
      setCookie("text-extractor-source", selectedSource);
    } else {
      delCookie("text-extractor-remember");
      delCookie("text-extractor-source");
    }
  };

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(extractedText);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Failed to copy", err);
      showNotice("Не удалось скопировать текст в буфер обмена.");
    }
  };

  const handleDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
  };

  const acceptFile = (selected: File, autoStart: boolean = false) => {
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
  };

  const handleDrop = (
    e: DragEvent<HTMLDivElement>,
    autoStart: boolean = false,
  ) => {
    e.preventDefault();
    setIsDragging(false);
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      acceptFile(e.dataTransfer.files[0], autoStart);
    }
  };

  const handleFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      acceptFile(e.target.files[0]);
    }
  };

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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleNewFile = () => {
    setAppState("upload");
    setFile(null);
  };

  const handleStartExtraction = () => {
    setLastExtractedPage(1);
    setTotalPdfPages(null);
    setExtractedText("");
    setAppState("loading");
    setTriggerCount((prev) => prev + 1);
  };

  const handleResumeExtraction = () => {
    setAppState("loading");
    setTriggerCount((prev) => prev + 1);
  };

  const handleInstallEasyOcr = () => {
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
        setEasyOcrInstalling(false);
      })
      .catch((error) => {
        showNotice(noticeFromError(error).message);
        setEasyOcrInstalling(false);
      });
  };

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

  return (
    <div
      className="min-h-screen bg-gray-50 dark:bg-gray-950 flex flex-col font-sans transition-colors duration-300 relative"
      onDragOver={(e) => {
        if (appState !== "upload") handleDragOver(e);
      }}
      onDragLeave={(e) => {
        if (appState !== "upload") handleDragLeave(e);
      }}
      onDrop={(e) => {
        if (appState !== "upload") handleDrop(e, true);
      }}
    >
      <NoticeToast notice={notice} onClose={() => setNotice(null)} />
      <DragOverlay appState={appState} isDragging={isDragging} />

      <AppHeader
        appState={appState}
        file={file}
        isDragging={isDragging}
        selectedSource={selectedSource}
        showHeader={showHeader}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        onNewFile={handleNewFile}
        onOpenSidebar={() => setSidebarOpen(true)}
        onSourceSelect={handleSourceSelect}
      />

      <main className="flex-1 flex flex-col items-center px-4 md:px-8 py-6 md:py-8 w-full max-w-7xl mx-auto relative z-10 overflow-x-hidden">
        <div className="w-full max-w-[800px] flex flex-col transition-all duration-500 flex-1">
          <AnimatePresence mode="popLayout">
            {appState === "upload" && (
              <UploadPanel
                diagnostics={diagnostics}
                fileInputRef={fileInputRef}
                isDragging={isDragging}
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onDrop={handleDrop}
                onFileChange={handleFileChange}
              />
            )}
          </AnimatePresence>

          {appState === "configure" && (
            <ConfigurePanel onStartExtraction={handleStartExtraction} />
          )}

          {appState === "loading" && (
            <LoadingPanel
              extractionProgress={extractionProgress}
              onCancelExtraction={cancelExtraction}
            />
          )}

          {appState === "reading" && (
            <ReadingPanel
              copied={copied}
              extractedText={extractedText}
              extractionProgress={extractionProgress}
              file={file}
              isExtracting={isExtracting}
              lastExtractedPage={lastExtractedPage}
              totalPdfPages={totalPdfPages}
              onCancelExtraction={cancelExtraction}
              onCopy={handleCopy}
              onNewFile={handleNewFile}
              onResumeExtraction={handleResumeExtraction}
              onStartExtraction={handleStartExtraction}
            />
          )}
        </div>
      </main>

      <SettingsSidebar
        easyOcrInstalling={easyOcrInstalling}
        isOpen={sidebarOpen}
        llmKey={llmKey}
        llmModel={llmModel}
        llmProvider={llmProvider}
        pingUrl={pingUrl}
        rememberChoice={rememberChoice}
        selectedSource={selectedSource}
        themeMode={themeMode}
        onClose={() => setSidebarOpen(false)}
        onInstallEasyOcr={handleInstallEasyOcr}
        onRememberChange={handleRememberChange}
        onSourceSelect={handleSourceSelect}
        setLlmKey={setLlmKey}
        setLlmModel={setLlmModel}
        setLlmProvider={setLlmProvider}
        setPingUrl={setPingUrl}
        setThemeMode={setThemeMode}
      />
    </div>
  );
}
