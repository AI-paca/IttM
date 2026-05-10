import { useState } from "react";
import { useOcrApp } from "../../ocr/ocr-context";
import { AppHeader } from "../AppHeader";
import { SettingsSidebar } from "../SettingsSidebar";

export function NavigationArea() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const {
    appState,
    dragHandlers,
    engineControls,
    file,
    isDragging,
    onNewFile,
    showHeader,
  } = useOcrApp();

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
        easyOcrInstalling={engineControls.easyOcrInstalling}
        isOpen={sidebarOpen}
        llmKey={engineControls.llmKey}
        llmModel={engineControls.llmModel}
        llmProvider={engineControls.llmProvider}
        pingUrl={engineControls.pingUrl}
        rememberChoice={engineControls.rememberChoice}
        selectedSource={engineControls.selectedSource}
        themeMode={engineControls.themeMode}
        onClose={() => setSidebarOpen(false)}
        onInstallEasyOcr={engineControls.onInstallEasyOcr}
        onRememberChange={engineControls.onRememberChange}
        onSourceSelect={engineControls.onSourceSelect}
        setLlmKey={engineControls.setLlmKey}
        setLlmModel={engineControls.setLlmModel}
        setLlmProvider={engineControls.setLlmProvider}
        setPingUrl={engineControls.setPingUrl}
        setThemeMode={engineControls.setThemeMode}
      />
    </>
  );
}
