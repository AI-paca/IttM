import { useState } from "react";
import { useEngineControls, useNavigationArea } from "../../ocr/ocr-context";
import { AppHeader } from "../AppHeader";
import { SettingsSidebar } from "../SettingsSidebar";

export function NavigationArea() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const { appState, dragHandlers, file, isDragging, onNewFile, showHeader } =
    useNavigationArea();
  const engineControls = useEngineControls();

  return (
    <>
      <AppHeader
        appState={appState}
        file={file}
        isDragging={isDragging}
        selectedSource={engineControls.selectedSource}
        showHeader={showHeader}
        onDragOver={dragHandlers.onDragOver}
        onDragLeave={dragHandlers.onDragLeave}
        onDrop={dragHandlers.onDrop}
        onNewFile={onNewFile}
        onOpenSidebar={() => setSidebarOpen(true)}
        onSourceSelect={engineControls.onSourceSelect}
      />

      <SettingsSidebar
        controls={engineControls}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
      />
    </>
  );
}
