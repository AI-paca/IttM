export type ProviderErrorCode =
  | "CIRCUIT_OPEN"
  | "INVALID_RESPONSE"
  | "NETWORK"
  | "RATE_LIMITED"
  | "TIMEOUT"
  | "UNAUTHORIZED"
  | "FORBIDDEN"
  | "UPSTREAM";

export class ProviderError extends Error {
  constructor(
    readonly code: ProviderErrorCode,
    message: string,
    readonly retryable: boolean,
  ) {
    super(message);
    this.name = "ProviderError";
  }
}

export interface TokenProvider {
  getToken(forceRefresh: boolean): Promise<string>;
}

export interface ProviderTransport {
  send(token: string, payload: string, signal: AbortSignal): Promise<Response>;
}

export class ProviderClient {
  private failures = 0;
  private openUntil = 0;

  constructor(
    private readonly tokens: TokenProvider,
    private readonly transport: ProviderTransport,
    private readonly options: {
      timeoutMs?: number;
      failureThreshold?: number;
      cooldownMs?: number;
      now?: () => number;
    } = {},
  ) {}

  async save(payload: string): Promise<Record<string, unknown>> {
    const now = (this.options.now || Date.now)();
    if (now < this.openUntil) {
      throw new ProviderError(
        "CIRCUIT_OPEN",
        "Provider circuit is temporarily open.",
        true,
      );
    }

    try {
      const result = await this.send(payload, false);
      this.failures = 0;
      return result;
    } catch (error) {
      const normalized =
        error instanceof ProviderError
          ? error
          : new ProviderError("NETWORK", String(error), true);
      if (normalized.retryable) {
        this.failures += 1;
        if (this.failures >= (this.options.failureThreshold ?? 3)) {
          this.openUntil = now + (this.options.cooldownMs ?? 30_000);
        }
      }
      throw normalized;
    }
  }

  private async send(
    payload: string,
    forceRefresh: boolean,
  ): Promise<Record<string, unknown>> {
    const token = await this.tokens.getToken(forceRefresh);
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      this.options.timeoutMs ?? 10_000,
    );
    let response: Response;

    try {
      response = await this.transport.send(token, payload, controller.signal);
    } catch (error) {
      if (controller.signal.aborted) {
        throw new ProviderError("TIMEOUT", "Provider request timed out.", true);
      }
      throw new ProviderError(
        "NETWORK",
        error instanceof Error ? error.message : String(error),
        true,
      );
    } finally {
      clearTimeout(timeout);
    }

    if (response.status === 401 && !forceRefresh) {
      return await this.send(payload, true);
    }
    if (response.status === 401) {
      throw new ProviderError(
        "UNAUTHORIZED",
        "Provider authorization was rejected.",
        false,
      );
    }
    if (response.status === 403) {
      throw new ProviderError(
        "FORBIDDEN",
        "Provider access was revoked or quota is unavailable.",
        false,
      );
    }
    if (response.status === 429) {
      throw new ProviderError(
        "RATE_LIMITED",
        "Provider rate limit was reached.",
        true,
      );
    }
    if (!response.ok) {
      throw new ProviderError(
        "UPSTREAM",
        `Provider returned HTTP ${response.status}.`,
        response.status >= 500,
      );
    }

    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
      throw new ProviderError(
        "INVALID_RESPONSE",
        "Provider returned a non-JSON success response.",
        true,
      );
    }
    try {
      return (await response.json()) as Record<string, unknown>;
    } catch {
      throw new ProviderError(
        "INVALID_RESPONSE",
        "Provider returned malformed JSON.",
        true,
      );
    }
  }
}
