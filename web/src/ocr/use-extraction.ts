import { useEffect, useRef } from "react";
import type { Dispatch, SetStateAction } from "react";
import { processPdfIntelligently } from "../lib/pdf-parser";
import {
  buildApiUrl,
  buildBackendGatewayCandidates,
  executeBackendOcr,
  executeBackendOcrWithFallback,
  noticeFromError,
} from "./api-client";
import {
  createBrowserOcrProfile,
  releaseBrowserOcrCache,
  runBrowserOcrLowMemory,
} from "./browser-engine";
import { base64JpegToFile } from "./file-utils";
import { executeLlmOcr } from "./llm-client";
import type {
  AppDiagnostics,
  AppState,
  LlmProvider,
  OcrResult,
  SourceType,
} from "./types";

interface UseOcrExtractionArgs {
  appState: AppState;
  diagnostics: AppDiagnostics | null;
  extractedText: string;
  file: File | null;
  lastExtractedPage: number;
  llmKey: string;
  llmModel: string;
  llmProvider: LlmProvider;
  pingUrl: string;
  selectedSource: SourceType;
  triggerCount: number;
  setAppState: Dispatch<SetStateAction<AppState>>;
  setExtractedText: Dispatch<SetStateAction<string>>;
  setExtractionProgress: Dispatch<SetStateAction<string>>;
  setIsExtracting: Dispatch<SetStateAction<boolean>>;
  setLastExtractedPage: Dispatch<SetStateAction<number>>;
  setTotalPdfPages: Dispatch<SetStateAction<number | null>>;
  showNotice: (message: string, tone?: "error" | "success") => void;
}

export function useOcrExtraction({
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
}: UseOcrExtractionArgs) {
  const activeExtractRef = useRef({ current: false });
  const previousFileRef = useRef<File | null>(null);

  const cancelExtraction = () => {
    activeExtractRef.current.current = false;
    setIsExtracting(false);
    void releaseBrowserOcrCache();
    if (!extractedText) {
      setAppState("configure");
    }
  };

  useEffect(() => {
    if (previousFileRef.current === file) return;

    previousFileRef.current = file;
    activeExtractRef.current.current = false;
    setIsExtracting(false);
    void releaseBrowserOcrCache();
  }, [file, setIsExtracting]);

  useEffect(() => {
    if (triggerCount === 0 || !file) return;

    const active = { current: true };
    activeExtractRef.current = active;

    const runExtract = async () => {
      console.log(
        "[OCR] Started runExtract. Source:",
        selectedSource,
        "File:",
        file.name,
      );
      setIsExtracting(true);
      try {
        let result: OcrResult | null = null;
        let progressiveText = lastExtractedPage > 1 ? extractedText || "" : "";
        const browserProfile = createBrowserOcrProfile(diagnostics);

        const handleChunk = (chunk: string, pageIndex?: number) => {
          console.log(
            "[OCR] handleChunk called. Chunk length:",
            chunk.length,
            "Page:",
            pageIndex,
          );
          if (!active.current) {
            console.log("[OCR] handleChunk ignored (active is false)");
            return;
          }
          progressiveText += chunk;
          setExtractedText(progressiveText);
          if (pageIndex !== undefined) {
            setLastExtractedPage(pageIndex + 1);
          }
          setAppState((prev) => (prev !== "reading" ? "reading" : prev));
        };

        const runBrowserFallback = async () => {
          if (file.type === "application/pdf") {
            const md = await processPdfIntelligently(
              file,
              (msg) => {
                if (active.current) setExtractionProgress(msg);
              },
              async (b64) => {
                const tempFile = await base64JpegToFile(b64);
                const ocrRes = await runBrowserOcrLowMemory(
                  tempFile,
                  (text) => {
                    if (active.current) setExtractionProgress(text);
                  },
                  undefined,
                  browserProfile,
                );
                return ocrRes.markdown;
              },
              handleChunk,
              lastExtractedPage,
              setTotalPdfPages,
              { renderScale: browserProfile.pdfRenderScale },
            );
            return { markdown: md };
          }
          return await runBrowserOcrLowMemory(
            file,
            (text) => {
              if (active.current) setExtractionProgress(text);
            },
            (chunk) => {
              handleChunk(chunk);
            },
            browserProfile,
          );
        };

        let effectiveSource = selectedSource;
        const localBackendAvailable =
          !diagnostics || Boolean(diagnostics.backend && !diagnostics.error);
        const autoBackendCandidates = buildBackendGatewayCandidates({
          customBaseUrl: pingUrl,
          includeLocal: localBackendAvailable,
        });

        if (effectiveSource === "auto" && autoBackendCandidates.length === 0) {
          console.log(
            "[OCR] No backend candidates are available, auto-switching to browser source",
          );
          effectiveSource = "browser";
        }

        if (effectiveSource === "browser") {
          result = await runBrowserFallback();
        } else if (
          effectiveSource === "local_tess" ||
          effectiveSource === "local_easy"
        ) {
          const engineType =
            effectiveSource === "local_tess" ? "tesseract" : "easyocr";
          const url = buildApiUrl("", "/api/convert", {
            engine_type: engineType,
          });

          result = await executeBackendOcr(file, url, active, (text) => {
            if (active.current) setExtractionProgress(text);
          });
          handleChunk(result.markdown || "");
        } else if (effectiveSource === "llm") {
          result = await executeLlmOcr(
            file,
            { provider: llmProvider, model: llmModel, key: llmKey },
            active,
            (text) => {
              if (active.current) setExtractionProgress(text);
            },
            handleChunk,
            lastExtractedPage,
            setTotalPdfPages,
            browserProfile.pdfRenderScale,
          );
        } else if (effectiveSource === "gateway") {
          const url = buildApiUrl(pingUrl, "/api/convert");
          result = await executeBackendOcr(file, url, active, (text) => {
            if (active.current) setExtractionProgress(text);
          });
          handleChunk(result.markdown || "");
        } else if (effectiveSource === "auto") {
          try {
            result = await executeBackendOcrWithFallback(
              file,
              autoBackendCandidates,
              active,
              (text) => {
                if (active.current) setExtractionProgress(text);
              },
            );
            handleChunk(result.markdown || "");
          } catch {
            if (llmKey.trim()) {
              setExtractionProgress(
                "Cloud/локальный gateway недоступен, пробуем LLM OCR...",
              );
              try {
                result = await executeLlmOcr(
                  file,
                  { provider: llmProvider, model: llmModel, key: llmKey },
                  active,
                  (text) => {
                    if (active.current) setExtractionProgress(text);
                  },
                  handleChunk,
                  lastExtractedPage,
                  setTotalPdfPages,
                  browserProfile.pdfRenderScale,
                );
              } catch (llmError) {
                console.warn("[OCR] LLM fallback failed:", llmError);
              }
            }

            if (!result) {
              if (active.current)
                setExtractionProgress(
                  "Cloud/локальный gateway недоступен, выполняем в браузере (WASM)...",
                );
              result = await runBrowserFallback();
            }
          }
        }

        if (active.current) {
          setAppState("reading");
          if (!progressiveText) {
            console.log(
              "[OCR] No chunks detected, using final result markdown",
            );
            setExtractedText(
              result?.markdown || "Не удалось распознать текст.",
            );
          }
        }
      } catch (error: unknown) {
        console.error("[OCR] Extraction failed:", error);
        const normalized = noticeFromError(error);
        if (active.current) {
          if (extractedText || appState === "reading") {
            setExtractedText(
              (prev) =>
                prev + `\n\n[Прервано из-за ошибки: ${normalized.message}]`,
            );
            showNotice(normalized.message);
          } else {
            showNotice(`Ошибка извлечения: ${normalized.message}`);
            setAppState("configure");
          }
        }
      } finally {
        console.log("[OCR] runExtract finished");
        if (active.current) setIsExtracting(false);
      }
    };
    runExtract();

    return () => {
      active.current = false;
    };
    // OCR should start only from explicit start/resume/file-paste triggers.
    // Switching source buttons while reading must not append another run.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [triggerCount]);

  return { cancelExtraction };
}
