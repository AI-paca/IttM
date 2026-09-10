export type RuntimeMode = "backend" | "lite";

export function resolveRuntimeMode(value: string | undefined): RuntimeMode {
  return value === "lite" ? "lite" : "backend";
}

const configuredMode = (
  import.meta as ImportMeta & { env?: { VITE_RUNTIME_MODE?: string } }
).env?.VITE_RUNTIME_MODE;

export const RUNTIME_MODE = resolveRuntimeMode(configuredMode);
export const IS_LITE_RUNTIME = RUNTIME_MODE === "lite";
