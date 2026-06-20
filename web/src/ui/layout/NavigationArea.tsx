import { useState } from "react";
import { useEngineControls, useNavigationArea } from "../../ocr/ocr-context";
import { AppHeader } from "../AppHeader";
import { SettingsSidebar } from "../SettingsSidebar";
import { useSettingsPullDownGesture } from "./useSettingsPullDownGesture";

export function NavigationArea() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const {
    activeSource,
    appState,
    dragHandlers,
    file,
    isDragging,
    onNewFile,
    showHeader,
  } = useNavigationArea();
  const engineControls = useEngineControls();

  useSettingsPullDownGesture({
    enabled:
      !sidebarOpen && (appState === "upload" || appState === "configure"),
    onOpen: () => setSidebarOpen(true),
  });

  return (
    <>
      <AppHeader
        appState={appState}
        file={file}
        isDragging={isDragging}
        selectedSource={activeSource ?? engineControls.selectedSource}
        showHeader={showHeader}
        onDragOver={dragHandlers.onDragOver}
        onDragLeave={dragHandlers.onDragLeave}
        onDrop={dragHandlers.onDrop}
        onNewFile={onNewFile}
        onOpenSidebar={() => setSidebarOpen(true)}
      />

      <SettingsSidebar
        controls={engineControls}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
      />
    </>
  );
}
