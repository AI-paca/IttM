interface BackendAvailability {
  backend: unknown;
  error?: string;
}

export function hasAvailableLocalBackend(
  diagnostics: BackendAvailability | null,
): boolean {
  return Boolean(diagnostics?.backend && !diagnostics.error);
}

export function shouldIncludeLocalBackend(
  diagnostics: BackendAvailability | null,
  isLiteRuntime: boolean,
): boolean {
  return !isLiteRuntime || hasAvailableLocalBackend(diagnostics);
}
