import { Activity, Cpu } from "lucide-react";
import type { AppDiagnostics, BackendGpuInfo } from "../../ocr/types";

interface DiagnosticsPanelProps {
  diagnostics: AppDiagnostics;
}

/**
 * Панель диагностики системы: локальная память/CPU + бэкенд RAM/CPU/GPU.
 */
export function DiagnosticsPanel({ diagnostics }: DiagnosticsPanelProps) {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-2xl border border-gray-200 dark:border-gray-700 p-4 shadow-sm flex flex-col gap-3 transition-colors delay-100">
      <div className="flex justify-between items-center">
        <h3 className="text-sm font-bold text-gray-700 dark:text-gray-300 flex items-center gap-2">
          <Activity className="w-4 h-4 text-blue-500" /> Diagnostics & System
        </h3>

        {/* Placeholder: Continuity Camera (Phase 5) */}
        <button className="hidden md:flex items-center gap-1.5 px-3 py-1.5 bg-gray-100/50 hover:bg-gray-100 dark:bg-gray-700/30 dark:hover:bg-gray-700 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 text-[11px] font-bold rounded-lg border border-transparent hover:border-gray-200 dark:hover:border-gray-600 transition-all cursor-not-allowed">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            className="w-4 h-4 opacity-70"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <rect width="14" height="20" x="5" y="2" rx="2" ry="2" />
            <path d="M12 18h.01" />
          </svg>
          Отсканировать с iPhone
        </button>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs text-gray-600 dark:text-gray-400">
        <Metric
          label="Local Memory"
          value={`${diagnostics.browser.memory} GB`}
        />
        <Metric
          label="Local CPU"
          value={`${diagnostics.browser.cores} Cores`}
        />
        {diagnostics.backend ? (
          <BackendMetrics diagnostics={diagnostics} />
        ) : (
          <div className="col-span-2 flex items-center bg-red-50 dark:bg-red-900/10 text-red-500 p-2.5 rounded-lg border border-red-100 dark:border-red-900/50 font-medium">
            Backend offline
          </div>
        )}
      </div>
    </div>
  );
}

function Metric({
  label,
  value,
  variant = "default",
}: {
  label: string;
  value: string;
  variant?: "default" | "backend";
}) {
  const tone =
    variant === "backend"
      ? "bg-blue-50 dark:bg-blue-900/20 border-blue-100 dark:border-blue-900/50"
      : "bg-gray-50 dark:bg-gray-900 border-gray-100 dark:border-gray-700/50";
  const labelTone =
    variant === "backend"
      ? "text-blue-800 dark:text-blue-200"
      : "text-gray-800 dark:text-gray-200";
  return (
    <div className={`flex flex-col p-2.5 rounded-lg border ${tone}`}>
      <span className={`font-semibold mb-0.5 ${labelTone}`}>{label}</span>
      {value}
    </div>
  );
}

function BackendMetrics({ diagnostics }: { diagnostics: AppDiagnostics }) {
  const backend = diagnostics.backend!;
  return (
    <>
      <Metric
        label="Backend RAM"
        value={`${backend.memory_used_gb} / ${backend.memory_total_gb} GB`}
        variant="backend"
      />
      <Metric
        label="Backend System"
        value={`${backend.system} / ${backend.cpu_cores} Cores`}
        variant="backend"
      />
      {backend.gpus && backend.gpus.length > 0 ? (
        <div className="col-span-2 sm:col-span-4 flex gap-2 flex-wrap mt-1">
          {backend.gpus.map((g: BackendGpuInfo, i: number) => (
            <div
              key={i}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-indigo-50 dark:bg-indigo-900/20 rounded-lg border border-indigo-100 dark:border-indigo-900/50 font-medium text-xs text-indigo-700 dark:text-indigo-300"
            >
              <Cpu className="w-3.5 h-3.5" />
              {g.name} {g.version && `(v${g.version})`}
            </div>
          ))}
        </div>
      ) : (
        <div className="col-span-2 sm:col-span-4 flex gap-2 flex-wrap mt-1">
          <div
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg border font-medium text-xs ${
              backend.gpu_error
                ? "bg-amber-50 dark:bg-amber-900/20 border-amber-200 dark:border-amber-800 text-amber-600 dark:text-amber-400"
                : "bg-gray-50 dark:bg-gray-900/20 border-gray-200 dark:border-gray-800 text-gray-500 dark:text-gray-400"
            }`}
          >
            <Cpu className="w-3.5 h-3.5" />
            {backend.gpu_error
              ? `GPU Error: ${backend.gpu_error}`
              : "No GPU Detected (CPU Mode)"}
          </div>
        </div>
      )}
    </>
  );
}
