import { useOcrShell } from "../ocr/ocr-context";
import { DragOverlay } from "./DragOverlay";
import { NavigationArea } from "./layout/NavigationArea";
import { NoticeToast } from "./NoticeToast";
import { OcrWorkspace } from "./workspace/OcrWorkspace";

export function AppShell() {
  const { appState, closeNotice, dragHandlers, isDragging, notice } =
    useOcrShell();

  return (
    <div
      className="min-h-screen bg-app text-primary flex flex-col font-sans transition-colors duration-300 relative"
      onDragOver={(event) => {
        if (appState !== "upload") dragHandlers.onDragOver(event);
      }}
      onDragLeave={(event) => {
        if (appState !== "upload") dragHandlers.onDragLeave(event);
      }}
      onDrop={(event) => {
        if (appState !== "upload") dragHandlers.onDrop(event, true);
      }}
    >
      <NoticeToast notice={notice} onClose={closeNotice} />
      <DragOverlay appState={appState} isDragging={isDragging} />
      <NavigationArea />
      <OcrWorkspace />
    </div>
  );
}
