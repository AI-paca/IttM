import { useEffect, useRef } from "react";
import type { Dispatch, SetStateAction } from "react";
import { processPdfIntelligently } from "../lib/pdf-parser";
import { effectivePdfCropMode, readCropMode } from "../lib/crop-preference";
import { debugMarkdownForFile } from "./debug-sample-markdown";
import {
  buildApiUrl,
  buildBackendGatewayCandidates,
  executeBackendOcrWithFallback,
  executeBackendOcrStreaming,
  isOllamaBaseUrl,
  normalizePlatformError,
  noticeFromError,
} from "./api-client";
import {
  createBrowserOcrProfile,
  releaseBrowserOcrCache,
  runBrowserOcrLowMemory,
} from "./browser-engine";
import { executeLlmOcr, executeOllamaOcr } from "./llm-client";
import {
  backendPipelineParams,
  browserPipelineProfileForSource,
} from "./pipeline-config";
import { hasAvailableLocalBackend } from "./source-availability";
import type {
  AppDiagnostics,
  ExtractionDocumentProgress,
  LlmProvider,
  OcrResult,
  ProgressDetail,
  ProgressSink,
  SourceType,
} from "./types";
import type { AppState } from "../types/app.types";

function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value));
}

function normalizeProgressPercent(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return null;
  }
  return clamp01(value > 1 ? value / 100 : value);
}

function normalizePageNumber(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return null;
  }
  return Math.max(1, Math.floor(value));
}

function normalizeCompletedPages(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return null;
  }
  return Math.max(0, Math.floor(value));
}

function hasProgressField(detail: ProgressDetail, key: keyof ProgressDetail) {
  return Object.prototype.hasOwnProperty.call(detail, key);
}

function calculateDocumentPercent(
  totalPages: number | null,
  completedPages: number,
  currentPagePercent: number | null,
) {
  if (!totalPages) return null;
  const completed = Math.min(Math.max(0, completedPages), totalPages);
  const current = currentPagePercent === null ? 0 : currentPagePercent;
  return clamp01((completed + current) / totalPages);
}

interface UseOcrExtractionArgs {
  diagnostics: AppDiagnostics | null;
  extractedText: string;
  file: File | null;
  lastExtractedPage: number;
  totalPdfPages: number | null;
  externalLlmConsent: boolean;
  llmKey: string;
  llmModel: string;
  llmProvider: LlmProvider;
  pingUrl: string;
  selectedSource: SourceType;
  triggerCount: number;
  setAppState: Dispatch<SetStateAction<AppState>>;
  setExtractedText: Dispatch<SetStateAction<string>>;
  setExtractionProgress: Dispatch<SetStateAction<string>>;
  setDocumentProgress: Dispatch<
    SetStateAction<ExtractionDocumentProgress | null>
  >;
  setActiveSource: Dispatch<SetStateAction<SourceType | null>>;
  setIsExtracting: Dispatch<SetStateAction<boolean>>;
  setLastExtractedPage: Dispatch<SetStateAction<number>>;
  setTotalPdfPages: Dispatch<SetStateAction<number | null>>;
  showNotice: (message: string, tone?: "error" | "success") => void;
}

export function useOcrExtraction({
  diagnostics,
  extractedText,
  file,
  lastExtractedPage,
  totalPdfPages,
  externalLlmConsent,
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
      let knownTotalPages = file.type === "application/pdf" ? totalPdfPages : 1;
      const initialCurrentPage =
        file.type === "application/pdf" ? Math.max(1, lastExtractedPage) : 1;
      const initialCompletedPages =
        file.type === "application/pdf"
          ? Math.max(0, lastExtractedPage - 1)
          : 0;
      setDocumentProgress({
        currentPage: initialCurrentPage,
        totalPages: knownTotalPages,
        completedPages: initialCompletedPages,
        currentPagePercent: null,
        documentPercent: calculateDocumentPercent(
          knownTotalPages,
          initialCompletedPages,
          null,
        ),
      });
      const updateDocumentProgress = (detail: ProgressDetail) => {
        if (!active.current) return;
        setDocumentProgress((previous) => {
          const fallback: ExtractionDocumentProgress = previous ?? {
            currentPage: initialCurrentPage,
            totalPages: knownTotalPages,
            completedPages: initialCompletedPages,
            currentPagePercent: null,
            documentPercent: calculateDocumentPercent(
              knownTotalPages,
              initialCompletedPages,
              null,
            ),
          };
          const currentPage = hasProgressField(detail, "currentPage")
            ? normalizePageNumber(detail.currentPage)
            : fallback.currentPage;
          const totalPages = hasProgressField(detail, "totalPages")
            ? normalizePageNumber(detail.totalPages)
            : fallback.totalPages;
          const completedPages =
            hasProgressField(detail, "completedPages") &&
            detail.completedPages !== undefined
              ? (normalizeCompletedPages(detail.completedPages) ??
                fallback.completedPages)
              : fallback.completedPages;
          const currentPagePercent = hasProgressField(
            detail,
            "currentPagePercent",
          )
            ? normalizeProgressPercent(detail.currentPagePercent)
            : fallback.currentPagePercent;
          const documentPercent = hasProgressField(detail, "documentPercent")
            ? normalizeProgressPercent(detail.documentPercent)
            : calculateDocumentPercent(
                totalPages,
                completedPages,
                currentPagePercent,
              );

          return {
            currentPage,
            totalPages,
            completedPages,
            currentPagePercent,
            documentPercent,
          };
        });
      };
      const setProgress: ProgressSink = (message, percent, detail) => {
        if (!active.current) return;
        setExtractionProgress(message);
        if (typeof percent === "number" || detail) {
          const incomingTotalPages = normalizePageNumber(detail?.totalPages);
          if (incomingTotalPages) {
            knownTotalPages = incomingTotalPages;
            if (file.type === "application/pdf") {
              setTotalPdfPages(incomingTotalPages);
            }
          }
          updateDocumentProgress({
            ...(typeof percent === "number"
              ? { currentPagePercent: percent }
              : {}),
            ...detail,
          });
        }
      };
      const rememberTotalPdfPages = (total: number) => {
        knownTotalPages = normalizePageNumber(total);
        if (!knownTotalPages) return;
        setTotalPdfPages(knownTotalPages);
        updateDocumentProgress({ totalPages: knownTotalPages });
      };
      let progressiveText = lastExtractedPage > 1 ? extractedText || "" : "";
      try {
        let result: OcrResult | null = null;
        const browserProfile = createBrowserOcrProfile(
          diagnostics,
          browserPipelineProfileForSource("browser"),
        );
        const pdfCropMode = effectivePdfCropMode(readCropMode());
        const activateSource = (source: SourceType) => {
          if (active.current) setActiveSource(source);
        };

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
            updateDocumentProgress({
              currentPage: pageIndex,
              ...(knownTotalPages ? { totalPages: knownTotalPages } : {}),
              completedPages: pageIndex,
              currentPagePercent: null,
            });
          } else if (file.type !== "application/pdf") {
            updateDocumentProgress({
              currentPage: 1,
              totalPages: 1,
              completedPages: 1,
              currentPagePercent: null,
            });
          }
          setAppState((prev) => (prev !== "reading" ? "reading" : prev));
        };

        const runBrowserFallback = async () => {
          activateSource("browser");
          if (file.type === "application/pdf") {
            const md = await processPdfIntelligently(
              file,
              (msg, detail) => {
                setProgress(msg, undefined, detail);
              },
              async (image, pageNumber, totalPages) => {
                const tempFile = new File([image], "page.jpg", {
                  type: image.type || "image/jpeg",
                });
                const pageProgress: ProgressSink = (text, percent, detail) => {
                  setProgress(text, percent, {
                    currentPage: pageNumber,
                    totalPages,
                    completedPages: pageNumber - 1,
                    ...(typeof percent === "number"
                      ? { currentPagePercent: percent }
                      : {}),
                    ...detail,
                  });
                };
                const ocrRes = await runBrowserOcrLowMemory(
                  tempFile,
                  pageProgress,
                  undefined,
                  browserProfile,
                );
                return ocrRes.markdown;
              },
              handleChunk,
              lastExtractedPage,
              rememberTotalPdfPages,
              {
                renderScale: browserProfile.pdfRenderScale,
                maxPagePixels: browserProfile.maxImagePixels,
                maxDimension: browserProfile.maxDimension,
                cropMode: pdfCropMode,
                shouldContinue: () => active.current,
              },
            );
            return { markdown: md };
          }
          return await runBrowserOcrLowMemory(
            file,
            setProgress,
            (chunk) => {
              handleChunk(chunk);
            },
            browserProfile,
          );
        };

        const debugMarkdown = debugMarkdownForFile(file);
        if (debugMarkdown) {
          activateSource("browser");
          setProgress("Показываем debug Markdown sample...");
          result = {
            markdown: debugMarkdown,
            meta: { debugFixture: true },
          };
        }

        if (!result) {
          let effectiveSource = selectedSource;
          const localBackendAvailable = hasAvailableLocalBackend(diagnostics);
          const autoBackendCandidates = buildBackendGatewayCandidates({
            customBaseUrl: pingUrl,
            includeLocal: localBackendAvailable,
          });

          if (
            effectiveSource === "auto" &&
            autoBackendCandidates.length === 0
          ) {
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
            activateSource(effectiveSource);
            const engineType =
              effectiveSource === "local_tess" ? "tesseract" : "easyocr";
            const url = buildApiUrl("", "/api/convert/stream", {
              engine_type: engineType,
              ...(backendPipelineParams(effectiveSource) || {}),
            });

            result = await executeBackendOcrStreaming(
              file,
              url,
              active,
              setProgress,
              handleChunk,
            );
          } else if (effectiveSource === "llm") {
            activateSource("llm");
            result = await executeLlmOcr(
              file,
              {
                provider: llmProvider,
                model: llmModel,
                key: llmKey,
                externalConsent: externalLlmConsent,
              },
              active,
              setProgress,
              handleChunk,
              lastExtractedPage,
              rememberTotalPdfPages,
              browserProfile.pdfRenderScale,
            );
          } else if (effectiveSource === "gateway") {
            activateSource("gateway");
            if (isOllamaBaseUrl(pingUrl)) {
              result = await executeOllamaOcr(
                file,
                { baseUrl: pingUrl, model: llmModel },
                active,
                setProgress,
                handleChunk,
                lastExtractedPage,
                rememberTotalPdfPages,
                browserProfile.pdfRenderScale,
              );
              if (file.type !== "application/pdf")
                handleChunk(result.markdown || "");
            } else {
              const gatewayUrl = buildApiUrl(
                pingUrl,
                "/api/convert/stream",
                backendPipelineParams("gateway"),
              );
              result = await executeBackendOcrStreaming(
                file,
                gatewayUrl,
                active,
                setProgress,
                handleChunk,
              );
            }
          } else if (effectiveSource === "auto") {
            try {
              activateSource("gateway");
              result = await executeBackendOcrWithFallback(
                file,
                autoBackendCandidates,
                active,
                setProgress,
                backendPipelineParams("auto"),
                handleChunk,
                { stallTimeoutMs: 35_000 },
              );
            } catch (backendError) {
              const normalizedBackendError =
                normalizePlatformError(backendError);
              if (normalizedBackendError.code === "OCR_STREAM_STALLED") {
                progressiveText = "";
                setExtractedText("");
                setLastExtractedPage(1);
                if (active.current) {
                  setProgress("Gateway завис, выполняем в браузере (WASM)...");
                }
                result = await runBrowserFallback();
              } else if (normalizedBackendError.partialResult) {
                throw backendError;
              }
              if (!result && llmKey.trim() && externalLlmConsent) {
                activateSource("llm");
                setProgress(
                  "Cloud/локальный gateway недоступен, пробуем LLM OCR...",
                );
                try {
                  result = await executeLlmOcr(
                    file,
                    {
                      provider: llmProvider,
                      model: llmModel,
                      key: llmKey,
                      externalConsent: externalLlmConsent,
                    },
                    active,
                    setProgress,
                    handleChunk,
                    lastExtractedPage,
                    rememberTotalPdfPages,
                    browserProfile.pdfRenderScale,
                  );
                } catch (llmError) {
                  console.warn("[OCR] LLM fallback failed:", llmError);
                }
              }

              if (!result) {
                if (active.current)
                  setProgress(
                    "Cloud/локальный gateway недоступен, выполняем в браузере (WASM)...",
                  );
                result = await runBrowserFallback();
              }
            }
          }
        }

        if (active.current) {
          setAppState("reading");
          const resultPages =
            typeof result?.meta?.pages === "number" &&
            Number.isFinite(result.meta.pages)
              ? Math.max(1, Math.floor(result.meta.pages))
              : knownTotalPages;
          if (resultPages) {
            updateDocumentProgress({
              currentPage: resultPages,
              totalPages: resultPages,
              completedPages: resultPages,
              currentPagePercent: null,
              documentPercent: 1,
            });
          }
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
          if (progressiveText) {
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
