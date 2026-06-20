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
 *
 * Рефакторинг: все Tailwind-цвета (gray/blue/amber) заменены на
 * семантические классы (.input-field, .btn-secondary, .warning-notice)
 * и токены из ui/theme/tokens.css.
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
    <div className="settings-panel">
      <div className="flex flex-col gap-1.5">
        <label className="form-label">Провайдер</label>
        <select
          value={llmProvider}
          onChange={(e) => {
            const prov = e.target.value as LlmProvider;
            onLlmProviderChange(prov);
            if (prov === "gemini") setLlmModel("gemini-2.5-flash-lite");
            else setLlmModel("baidu/qianfan-ocr-fast:free");
          }}
          className="input-field"
        >
          <option value="gemini">Google Gemini</option>
          <option value="openrouter">OpenRouter</option>
        </select>
      </div>

      <div className="flex flex-col gap-1.5">
        <label className="form-label">Модель</label>
        <input
          type="text"
          value={llmModel}
          onChange={(e) => setLlmModel(e.target.value)}
          className="input-field font-mono"
        />
      </div>

      <div className="flex flex-col gap-1.5">
        <label className="form-label">API Ключ</label>
        <div className="flex gap-2">
          <input
            type="password"
            value={llmKey}
            onChange={(e) => setLlmKey(e.target.value)}
            placeholder="Введите ключ..."
            className="input-field flex-1 min-w-0 font-mono"
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
            className="btn-secondary"
            title="Вставить"
          >
            <ClipboardPaste className="w-4 h-4" />
          </button>
        </div>
      </div>

      <label className="warning-notice">
        <input
          type="checkbox"
          checked={externalLlmConsent}
          onChange={(event) => setExternalLlmConsent(event.target.checked)}
          className="mt-0.5 h-4 w-4 shrink-0 accent-accent"
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
