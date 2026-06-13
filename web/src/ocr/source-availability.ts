interface BackendAvailability {
  backend: unknown;
  error?: string;
}

export function hasAvailableLocalBackend(
  diagnostics: BackendAvailability | null,
): boolean {
  return Boolean(diagnostics?.backend && !diagnostics.error);
}
