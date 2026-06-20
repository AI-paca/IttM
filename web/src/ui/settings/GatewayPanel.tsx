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
 */
export function GatewayPanel({ pingUrl, setPingUrl }: GatewayPanelProps) {
  const value = GATEWAY_OPTIONS.includes(pingUrl) ? pingUrl : "custom";

  return (
    <div className="mt-1 flex flex-col gap-2 px-1">
      <select
        onChange={(e) => {
          const v = e.target.value;
          setPingUrl(v === "custom" ? "" : v);
        }}
        className="p-2 border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 rounded-lg text-sm text-gray-800 dark:text-gray-200 outline-none focus:ring-2 focus:ring-blue-500 transition-all font-sans"
        value={value}
      >
        <option value="">Текущий Gateway</option>
        <option value="http://localhost:11434">
          Напрямую в Ollama (:11434)
        </option>
        <option value="custom">Cloudflare Edge / Custom</option>
      </select>
      {value === "custom" && (
        <input
          type="url"
          placeholder="Edge / Custom URL: https://..."
          value={pingUrl}
          onChange={(e) => setPingUrl(e.target.value)}
          className="w-full p-2.5 border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 rounded-xl text-xs sm:text-sm focus:ring-2 focus:ring-blue-500 outline-none text-gray-800 dark:text-gray-200 transition-all font-mono shadow-sm"
        />
      )}
    </div>
  );
}
