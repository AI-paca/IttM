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
    <div className="bg-[var(--color-bg-surface)] rounded-2xl border border-[var(--color-border-default)] p-4 shadow-sm flex flex-col gap-3 transition-colors delay-100">
      <div className="flex justify-between items-center">
        <h3 className="text-sm font-bold text-[var(--color-text-primary)] flex items-center gap-2">
          <Activity className="w-4 h-4 text-[var(--color-info)]" /> Diagnostics
          & System
        </h3>

        {/* Placeholder: Continuity Camera (Phase 5) */}
        <button className="hidden md:flex items-center gap-1.5 px-3 py-1.5 bg-[var(--color-bg-inset)] text-[var(--color-text-muted)] text-[11px] font-bold rounded-lg border border-transparent hover:border-[var(--color-border-default)] transition-all cursor-not-allowed">
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
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs text-[var(--color-text-secondary)]">
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
          <div className="col-span-2 flex items-center bg-[var(--color-danger-soft)] text-[var(--color-danger-text)] p-2.5 rounded-lg border border-[var(--color-danger-border)] font-medium">
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
      ? "bg-[var(--color-info-soft)] border-[var(--color-info-border)]"
      : "bg-[var(--color-bg-inset)] border-[var(--color-border-subtle)]";
  const labelTone =
    variant === "backend"
      ? "text-[var(--color-info-text)]"
      : "text-[var(--color-text-primary)]";
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
              className="flex items-center gap-1.5 px-3 py-1.5 bg-[var(--color-info-soft)] rounded-lg border border-[var(--color-info-border)] font-medium text-xs text-[var(--color-info-text)]"
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
                ? "bg-[var(--color-warning-soft)] border-[var(--color-warning-border)] text-[var(--color-warning-text)]"
                : "bg-[var(--color-bg-inset)] border-[var(--color-border-default)] text-[var(--color-text-muted)]"
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
