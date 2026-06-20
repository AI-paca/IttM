import { ClipboardPaste } from "lucide-react";
import type { EngineControls } from "../layout/engine-controls.types";
import type { LlmProvider } from "../../ocr/types";

interface LlmPanelProps {
  controls: EngineControls;
}

/**
 * Панель настройки внешнего LLM-провайдера (Gemini / OpenRouter).
 * Включает обязательную галочку явного согласия на отправку данных
 * во внешний сервис — действует только до перезагрузки вкладки.
 */
export function LlmPanel({ controls }: LlmPanelProps) {
  const {
    externalLlmConsent,
    llmKey,
    llmModel,
    llmProvider,
    onLlmProviderChange,
    setLlmKey,
    setLlmModel,
    setExternalLlmConsent,
  } = controls;

  return (
    <div className="mt-1 flex flex-col gap-3 px-1 border border-gray-200 dark:border-gray-700 rounded-xl p-3 bg-white dark:bg-gray-800 shadow-sm mx-1">
      <div className="flex flex-col gap-1.5">
        <label className="text-[11px] font-bold text-gray-500 dark:text-gray-400">
          Провайдер
        </label>
        <select
          value={llmProvider}
          onChange={(e) => {
            const prov = e.target.value as LlmProvider;
            onLlmProviderChange(prov);
            if (prov === "gemini") setLlmModel("gemini-2.5-flash-lite");
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
                const text = await navigator.clipboard.readText();
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
      <label className="flex items-start gap-2.5 rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-amber-950 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-100">
        <input
          type="checkbox"
          checked={externalLlmConsent}
          onChange={(event) => setExternalLlmConsent(event.target.checked)}
          className="mt-0.5 h-4 w-4 shrink-0 accent-blue-600"
        />
        <span className="text-[11px] leading-4">
          Я согласен отправить содержимое документа во внешний сервис{" "}
          {llmProvider === "gemini" ? "Google Gemini" : "OpenRouter"}. Согласие
          действует только до перезагрузки вкладки.
        </span>
      </label>
    </div>
  );
}
