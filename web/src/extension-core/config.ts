export interface SelectorConfig {
  version: number;
  selectors: Record<string, string>;
}

const SAFE_SELECTOR = /^[A-Za-z0-9_#.:[\]="'\-\s>+~*()]+$/;

export function resolveFeatureVariant<T extends string>(
  value: unknown,
  allowed: readonly T[],
  fallback: T,
): T {
  return typeof value === "string" && allowed.includes(value as T)
    ? (value as T)
    : fallback;
}

export function validateSelectorConfig(value: unknown): SelectorConfig {
  if (!value || typeof value !== "object") {
    throw new TypeError("Selector config must be an object.");
  }
  const candidate = value as Partial<SelectorConfig>;
  if (!Number.isInteger(candidate.version) || Number(candidate.version) < 1) {
    throw new TypeError("Selector config version must be a positive integer.");
  }
  if (!candidate.selectors || typeof candidate.selectors !== "object") {
    throw new TypeError("Selector config must contain selectors.");
  }

  const selectors: Record<string, string> = {};
  for (const [name, selector] of Object.entries(candidate.selectors)) {
    if (
      !name ||
      typeof selector !== "string" ||
      selector.length === 0 ||
      selector.length > 500 ||
      !SAFE_SELECTOR.test(selector) ||
      selector.includes(">>") ||
      selector.toLowerCase().includes("javascript:")
    ) {
      throw new TypeError(`Invalid selector config entry '${name}'.`);
    }
    selectors[name] = selector;
  }
  return { version: Number(candidate.version), selectors };
}

export async function loadSelectorConfig(
  fetchRemote: () => Promise<unknown>,
  builtIn: SelectorConfig,
): Promise<SelectorConfig> {
  try {
    return validateSelectorConfig(await fetchRemote());
  } catch {
    return builtIn;
  }
}

export function isTokenFresh(
  expiresAt: number,
  now = Date.now(),
  skewMs = 30_000,
): boolean {
  return (
    Number.isFinite(expiresAt) &&
    Number.isFinite(now) &&
    expiresAt - skewMs > now
  );
}
