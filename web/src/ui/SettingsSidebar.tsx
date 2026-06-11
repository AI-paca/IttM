import {
  Check,
  ClipboardPaste,
  DownloadCloud,
  Moon,
  Sun,
  X,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import type { EngineControls } from "./layout/engine-controls.types";
import type { LlmProvider, SourceType } from "../ocr/types";
import { SOURCES } from "./sources";

interface SettingsSidebarProps {
  controls: EngineControls;
  isOpen: boolean;
  onClose: () => void;
}

export function SettingsSidebar({
  controls,
  isOpen,
  onClose,
}: SettingsSidebarProps) {
  const {
    easyOcrInstalling,
    easyOcrInstallMessage,
    easyOcrInstallProgress,
    llmKey,
    llmModel,
    llmProvider,
    pingUrl,
    rememberChoice,
    selectedSource,
    themeMode,
    onInstallEasyOcr,
    onRememberChange,
    onSourceSelect,
    setLlmKey,
    setLlmModel,
    setLlmProvider,
    setPingUrl,
    setThemeMode,
  } = controls;

  const localGatewayEndpoints = [""];
  const gatewayEndpointOptions = [
    ...localGatewayEndpoints,
    "http://localhost:11434",
    "http://127.0.0.1:11434",
  ];
  const gatewayEndpointValue = localGatewayEndpoints.includes(pingUrl)
    ? ""
    : gatewayEndpointOptions.includes(pingUrl)
      ? pingUrl
      : "custom";

  return (
    <AnimatePresence>
      {isOpen && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 bg-gray-900/40 z-[90] backdrop-blur-sm"
            onClick={onClose}
          />
          <motion.div
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ type: "tween", ease: "easeOut", duration: 0.15 }}
            className="fixed top-0 right-0 bottom-0 w-[85%] max-w-[340px] bg-white dark:bg-gray-900 shadow-2xl z-[100] border-l border-gray-200 dark:border-gray-800 flex flex-col font-sans"
          >
            <button
              onClick={onClose}
              className="absolute top-4 right-4 p-1.5 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 bg-gray-100/50 dark:bg-gray-800/50 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-full transition-colors z-10"
            >
              <X className="w-6 h-6" />
            </button>

            <div className="p-4 sm:p-5 pt-14 flex flex-col gap-6 overflow-y-auto">
              <div className="flex flex-col gap-4">
                <div className="flex flex-col gap-2">
                  <h3 className="text-[10px] font-bold text-gray-400 uppercase tracking-widest pl-1">
                    Local & Browser
                  </h3>
                  {SOURCES.filter((s) =>
                    ["auto", "browser", "local_tess", "local_easy"].includes(
                      s.id,
                    ),
                  ).map((src) => (
                    <button
                      key={src.id}
                      onClick={() => onSourceSelect(src.id as SourceType)}
                      className={`w-full flex items-center justify-between px-3 py-3 text-left transition-colors rounded-xl border ${
                        selectedSource === src.id
                          ? "bg-blue-50/50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800"
                          : "bg-white dark:bg-gray-800 border-gray-100 dark:border-gray-700 hover:border-blue-300 dark:hover:border-gray-600"
                      }`}
                    >
                      <div className="flex items-center gap-3">
                        <div
                          className={
                            selectedSource === src.id
                              ? "text-blue-600 dark:text-blue-400"
                              : "text-gray-400 dark:text-gray-500"
                          }
                        >
                          {src.icon}
                        </div>
                        <div className="flex flex-col">
                          <span
                            className={`text-[13px] font-semibold ${selectedSource === src.id ? "text-blue-700 dark:text-blue-300" : "text-gray-700 dark:text-gray-200"}`}
                          >
                            {src.label}
                          </span>
                          <span className="text-[11px] text-gray-500 dark:text-gray-400 leading-tight mt-0.5">
                            {src.desc}
                          </span>
                        </div>
                      </div>
                      <div className="flex flex-row items-center gap-2">
                        {src.id === "local_easy" &&
                          !easyOcrInstalling &&
                          selectedSource === src.id && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                onInstallEasyOcr();
                              }}
                              className="p-1.5 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg text-gray-400 hover:text-blue-500 hover:border-blue-300 dark:hover:border-blue-700 shadow-sm mr-2 active:scale-95 transition-all text-center"
                              title="Скачать EasyOCR (~5ГБ)"
                            >
                              <DownloadCloud className="w-4 h-4" />
                            </button>
                          )}
                        {src.id === "local_easy" && easyOcrInstalling && (
                          <div
                            className="w-[104px] px-2 py-1 bg-blue-50 dark:bg-blue-900 border border-blue-200 dark:border-blue-700 rounded text-[10px] text-blue-700 dark:text-blue-200 mr-2"
                            title={easyOcrInstallMessage}
                          >
                            <div className="flex items-center justify-between gap-1">
                              <span className="truncate">
                                {Math.round(easyOcrInstallProgress)}%
                              </span>
                              <div className="w-2 h-2 border-2 border-blue-500 border-t-transparent rounded-full animate-spin shrink-0"></div>
                            </div>
                            <div className="mt-1 h-1 overflow-hidden rounded-full bg-blue-100 dark:bg-blue-950">
                              <div
                                className="h-full rounded-full bg-blue-500 transition-all duration-500"
                                style={{
                                  width: `${Math.max(
                                    3,
                                    Math.min(100, easyOcrInstallProgress),
                                  )}%`,
                                }}
                              />
                            </div>
                          </div>
                        )}
                        {selectedSource === src.id && (
                          <Check className="w-4 h-4 text-blue-600 dark:text-blue-400 shrink-0" />
                        )}
                      </div>
                    </button>
                  ))}
                </div>

                <div className="flex flex-col gap-2 mt-2">
                  <h3 className="text-[10px] font-bold text-gray-400 uppercase tracking-widest pl-1">
                    API & Cloud
                  </h3>
                  {SOURCES.filter((s) => ["gateway", "llm"].includes(s.id)).map(
                    (src) => (
                      <button
                        key={src.id}
                        onClick={() => onSourceSelect(src.id as SourceType)}
                        className={`w-full flex items-center justify-between px-3 py-3 text-left transition-colors rounded-xl border ${
                          selectedSource === src.id
                            ? "bg-blue-50/50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800"
                            : "bg-white dark:bg-gray-800 border-gray-100 dark:border-gray-700 hover:border-blue-300 dark:hover:border-gray-600"
                        }`}
                      >
                        <div className="flex items-center gap-3">
                          <div
                            className={
                              selectedSource === src.id
                                ? "text-blue-600 dark:text-blue-400"
                                : "text-gray-400 dark:text-gray-500"
                            }
                          >
                            {src.icon}
                          </div>
                          <div className="flex flex-col">
                            <span
                              className={`text-[13px] font-semibold ${selectedSource === src.id ? "text-blue-700 dark:text-blue-300" : "text-gray-700 dark:text-gray-200"}`}
                            >
                              {src.label}
                            </span>
                            <span className="text-[11px] text-gray-500 dark:text-gray-400 leading-tight mt-0.5">
                              {src.desc}
                            </span>
                          </div>
                        </div>
                        {selectedSource === src.id && (
                          <Check className="w-4 h-4 text-blue-600 dark:text-blue-400 shrink-0" />
                        )}
                      </button>
                    ),
                  )}

                  {selectedSource === "gateway" && (
                    <div className="mt-1 flex flex-col gap-2 px-1">
                      <select
                        onChange={(e) => {
                          const value = e.target.value;
                          setPingUrl(value === "custom" ? "" : value);
                        }}
                        className="p-2 border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 rounded-lg text-sm text-gray-800 dark:text-gray-200 outline-none focus:ring-2 focus:ring-blue-500 transition-all font-sans"
                        value={gatewayEndpointValue}
                      >
                        <option value="">Текущий Gateway</option>
                        <option value="http://localhost:11434">
                          Напрямую в Ollama (:11434)
                        </option>
                        <option value="custom">Cloudflare Edge / Custom</option>
                      </select>
                      {gatewayEndpointValue === "custom" && (
                        <input
                          type="url"
                          placeholder="Edge / Custom URL: https://..."
                          value={pingUrl}
                          onChange={(e) => setPingUrl(e.target.value)}
                          className="w-full p-2.5 border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 rounded-xl text-xs sm:text-sm focus:ring-2 focus:ring-blue-500 outline-none text-gray-800 dark:text-gray-200 transition-all font-mono shadow-sm"
                        />
                      )}
                    </div>
                  )}

                  {selectedSource === "llm" && (
                    <div className="mt-1 flex flex-col gap-3 px-1 border border-gray-200 dark:border-gray-700 rounded-xl p-3 bg-white dark:bg-gray-800 shadow-sm mx-1">
                      <div className="flex flex-col gap-1.5">
                        <label className="text-[11px] font-bold text-gray-500 dark:text-gray-400">
                          Провайдер
                        </label>
                        <select
                          value={llmProvider}
                          onChange={(e) => {
                            const prov = e.target.value as LlmProvider;
                            setLlmProvider(prov);
                            if (prov === "gemini")
                              setLlmModel("gemini-2.5-flash-lite");
                            else setLlmModel("baidu/qianfan-ocr-fast:free");
                          }}
                          className="p-2 border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 rounded-lg text-sm text-gray-800 dark:text-gray-200 outline-none focus:ring-2 focus:ring-blue-500 transition-all"
                        >
                          <option value="gemini">Google Gemini</option>
                          <option value="openrouter">OpenRouter</option>
                        </select>
                      </div>
                      <div className="flex flex-col gap-1.5">
                        <label className="text-[11px] font-bold text-gray-500 dark:text-gray-400">
                          Модель
                        </label>
                        <input
                          type="text"
                          value={llmModel}
                          onChange={(e) => setLlmModel(e.target.value)}
                          className="p-2 border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 rounded-lg text-sm text-gray-800 dark:text-gray-200 outline-none focus:ring-2 focus:ring-blue-500 transition-all font-mono"
                        />
                      </div>
                      <div className="flex flex-col gap-1.5">
                        <label className="text-[11px] font-bold text-gray-500 dark:text-gray-400">
                          API Ключ
                        </label>
                        <div className="flex gap-2">
                          <input
                            type="password"
                            value={llmKey}
                            onChange={(e) => setLlmKey(e.target.value)}
                            placeholder="Введите ключ..."
                            className="flex-1 min-w-0 p-2 border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 rounded-lg text-sm text-gray-800 dark:text-gray-200 outline-none focus:ring-2 focus:ring-blue-500 transition-all font-mono"
                          />
                          <button
                            onClick={async () => {
                              try {
                                const text =
                                  await navigator.clipboard.readText();
                                setLlmKey(text);
                              } catch (e) {
                                console.debug("Clipboard read failed", e);
                              }
                            }}
                            className="p-2 bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-600 dark:text-gray-300 rounded-lg transition-colors border border-gray-200 dark:border-gray-700"
                            title="Вставить"
                          >
                            <ClipboardPaste className="w-4 h-4" />
                          </button>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              </div>

              <div className="flex-1" />

              <div className="h-px bg-gray-100 dark:bg-gray-800 mt-2" />

              <div className="flex flex-col gap-3 px-1 mt-2">
                <label className="relative flex items-center gap-3 cursor-pointer group">
                  <input
                    type="checkbox"
                    className="peer sr-only"
                    checked={rememberChoice}
                    onChange={(e) => onRememberChange(e.target.checked)}
                  />
                  <div className="w-5 h-5 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 peer-checked:bg-blue-600 peer-checked:border-blue-600 dark:peer-checked:bg-blue-600 dark:peer-checked:border-blue-600 transition-colors flex items-center justify-center shrink-0">
                    <svg
                      className="w-3.5 h-3.5 text-white opacity-0 peer-checked:opacity-100 transition-opacity"
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                      strokeWidth={3}
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M5 13l4 4L19 7"
                      />
                    </svg>
                  </div>
                  <span className="text-[14px] font-semibold text-gray-700 dark:text-gray-300 select-none group-hover:text-gray-900 dark:group-hover:text-gray-100 transition-colors">
                    Запомнить выбор (Cookies)
                  </span>
                </label>
              </div>

              <div className="flex flex-col gap-3 pb-4">
                <div className="flex bg-gray-100 dark:bg-gray-800 p-1 rounded-xl w-full">
                  <button
                    onClick={() => setThemeMode("auto")}
                    className={`flex-1 py-1.5 text-xs font-bold rounded-lg transition-all ${themeMode === "auto" ? "bg-white dark:bg-gray-700 shadow-sm text-gray-900 dark:text-white" : "text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300"}`}
                  >
                    Default
                  </button>
                  <button
                    onClick={() => setThemeMode("light")}
                    className={`flex-1 flex justify-center items-center py-1.5 text-xs font-bold rounded-lg transition-all ${themeMode === "light" ? "bg-white dark:bg-gray-700 shadow-sm text-gray-900 dark:text-white" : "text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300"}`}
                  >
                    <Sun className="w-4 h-4" />
                  </button>
                  <button
                    onClick={() => setThemeMode("dark")}
                    className={`flex-1 flex justify-center items-center py-1.5 text-xs font-bold rounded-lg transition-all ${themeMode === "dark" ? "bg-white dark:bg-gray-700 shadow-sm text-gray-900 dark:text-white" : "text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300"}`}
                  >
                    <Moon className="w-4 h-4" />
                  </button>
                </div>
              </div>
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
