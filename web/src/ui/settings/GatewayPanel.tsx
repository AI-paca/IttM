import { useRef } from "react";
import { PasteButton } from "./PasteButton";

interface GatewayPanelProps {
  pingUrl: string;
  setPingUrl: (url: string) => void;
}

const GATEWAY_OPTIONS = [
  "",
  "http://localhost:11434",
  "http://127.0.0.1:11434",
];

/**
 * Панель выбора endpoint для источника Gateway:
 * текущий gateway, прямое подключение к Ollama или кастомный URL (Edge).
 *
 * Рефакторинг: Tailwind-цвета заменены на .input-field из components.css.
 */
export function GatewayPanel({ pingUrl, setPingUrl }: GatewayPanelProps) {
  const value = GATEWAY_OPTIONS.includes(pingUrl) ? pingUrl : "custom";
  const customUrlRef = useRef<HTMLInputElement>(null);

  return (
    <div className="mt-1 flex flex-col gap-2 px-1">
      <select
        onChange={(e) => {
          const v = e.target.value;
          setPingUrl(v === "custom" ? "" : v);
        }}
        className="input-field"
        value={value}
      >
        <option value="">Текущий Gateway</option>
        <option value="http://localhost:11434">
          Напрямую в Ollama (:11434)
        </option>
        <option value="custom">Cloudflare Edge / Custom</option>
      </select>
      {value === "custom" && (
        <div className="flex gap-2">
          <input
            ref={customUrlRef}
            type="url"
            placeholder="Edge / Custom URL: https://..."
            value={pingUrl}
            onChange={(e) => setPingUrl(e.target.value)}
            className="input-field h-10 w-full min-w-0 text-xs sm:text-sm font-mono shadow-sm"
          />
          <PasteButton
            targetRef={customUrlRef}
            onPaste={setPingUrl}
            title="Вставить URL"
          />
        </div>
      )}
    </div>
  );
}
