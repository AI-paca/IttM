export const EXTERNAL_LLM_CONSENT_ERROR =
  "Перед отправкой документа во внешний LLM подтвердите согласие в настройках.";

export function assertExternalLlmConsent(consentGranted: boolean): void {
  if (!consentGranted) {
    throw new Error(EXTERNAL_LLM_CONSENT_ERROR);
  }
}

export async function runExternalLlmRequest<T>(
  consentGranted: boolean,
  request: () => Promise<T>,
): Promise<T> {
  assertExternalLlmConsent(consentGranted);
  return await request();
}
