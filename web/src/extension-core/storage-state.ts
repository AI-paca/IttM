export interface KeyValueStorage {
  get(key: string): Promise<string | undefined>;
  set(key: string, value: string): Promise<void>;
  remove(key: string): Promise<void>;
}

export async function readStoredJson<T>(
  storage: KeyValueStorage,
  key: string,
): Promise<T | null> {
  const raw = await storage.get(key);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

export async function saveWithDiagnosticCleanup(
  storage: KeyValueStorage,
  key: string,
  value: unknown,
  diagnosticKey = "diagnostics",
): Promise<void> {
  const serialized = JSON.stringify(value);
  try {
    await storage.set(key, serialized);
  } catch (error) {
    if (
      !(error instanceof DOMException) ||
      error.name !== "QuotaExceededError"
    ) {
      throw error;
    }
    await storage.remove(diagnosticKey);
    await storage.set(key, serialized);
  }
}
