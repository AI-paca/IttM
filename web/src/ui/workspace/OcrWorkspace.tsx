import { AnimatePresence } from "motion/react";
import { useOcrWorkspace } from "../../ocr/ocr-context";
import { ConfigurePanel } from "../ConfigurePanel";
import { LoadingPanel } from "../LoadingPanel";
import { ReadingPanel } from "../ReadingPanel";
import { UploadPanel } from "../UploadPanel";

export function OcrWorkspace() {
  const {
    appState,
    copied,
    diagnostics,
    documentProgress,
    dragHandlers,
    extractedText,
    extractionProgress,
    file,
    fileInputRef,
    isDragging,
    isExtracting,
    lastExtractedPage,
    onCancelExtraction,
    onCopy,
    onFileChange,
    onNewFile,
    onResumeExtraction,
    onStartExtraction,
    totalPdfPages,
  } = useOcrWorkspace();

  return (
    <main className="flex-1 flex flex-col items-center px-4 md:px-8 py-6 md:py-8 w-full max-w-7xl mx-auto relative z-10 overflow-x-hidden">
      <div
        className={`w-full flex flex-col transition-all duration-500 flex-1 ${
          appState === "reading" ? "max-w-5xl" : "max-w-[900px]"
        }`}
      >
        <AnimatePresence mode="popLayout">
          {appState === "upload" && (
            <UploadPanel
              diagnostics={diagnostics}
              fileInputRef={fileInputRef}
              isDragging={isDragging}
              onDragOver={dragHandlers.onDragOver}
              onDragLeave={dragHandlers.onDragLeave}
              onDrop={dragHandlers.onDrop}
              onFileChange={onFileChange}
            />
          )}
        </AnimatePresence>

        {appState === "configure" && (
          <ConfigurePanel onStartExtraction={onStartExtraction} />
        )}

        {appState === "loading" && (
          <LoadingPanel
            documentProgress={documentProgress}
            extractionProgress={extractionProgress}
            onCancelExtraction={onCancelExtraction}
          />
        )}

        {appState === "reading" && (
          <ReadingPanel
            copied={copied}
            documentProgress={documentProgress}
            extractedText={extractedText}
            extractionProgress={extractionProgress}
            file={file}
            isExtracting={isExtracting}
            lastExtractedPage={lastExtractedPage}
            totalPdfPages={totalPdfPages}
            onCancelExtraction={onCancelExtraction}
            onCopy={onCopy}
            onNewFile={onNewFile}
            onResumeExtraction={onResumeExtraction}
            onStartExtraction={onStartExtraction}
          />
        )}
      </div>
    </main>
  );
}
