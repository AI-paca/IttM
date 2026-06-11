import { cropWhiteBorders, processPdfIntelligently } from "../lib/pdf-parser";
import {
  buildOllamaGenerateUrl,
  parsePlatformError,
  normalizePlatformError,
} from "./api-client";
import { imageFileToCroppedBase64 } from "./file-utils";
import type { LlmProvider, OcrResult, ProgressSink } from "./types";

const OCR_PROMPT =
  "Extract all text from this image/document. Preserve tables as Markdown tables. Output only the extracted content, no markdown fences.";

interface ImportMetaWithEnv extends ImportMeta {
  env?: Record<string, string | undefined>;
}

function envValue(name: string): string {
  return ((import.meta as ImportMetaWithEnv).env?.[name] ?? "").trim();
}

function buildGeminiEdgeUrl(model: string): string | null {
  const configured = envValue("VITE_GEMINI_EDGE_URL").replace(/\/+$/, "");
  if (!configured) return null;

  if (configured.endsWith("/api/gemini")) {
    return `${configured}/v1beta/models/${encodeURIComponent(model)}:generateContent`;
  }

  return `${configured}/api/gemini/v1beta/models/${encodeURIComponent(
    model,
  )}:generateContent`;
}

function normalizeOllamaModel(model: string): string {
  if (!model || model.startsWith("gemini-") || model.includes("/")) {
    return "llava";
  }
  return model;
}

export interface LlmSettings {
  provider: LlmProvider;
  model: string;
  key: string;
}

export async function executeLlmOcrForImage(
  b64: string,
  mimeType: string,
  settings: LlmSettings,
  activeContent: { current: boolean },
  onProgress?: ProgressSink,
): Promise<OcrResult> {
  const key = settings.key.trim();
  const model = settings.model.trim();
  if (!model) throw new Error("Модель не указана");

  if (settings.provider === "gemini") {
    if (activeContent.current) onProgress?.("Запрос к Gemini...");
    const edgeUrl = key ? null : buildGeminiEdgeUrl(model);
    if (!key && !edgeUrl) throw new Error("API ключ Gemini не указан");

    const url =
      edgeUrl ??
      `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(
        model,
      )}:generateContent?key=${encodeURIComponent(key)}`;
    const payload = {
      contents: [
        {
          parts: [
            { text: OCR_PROMPT },
            { inlineData: { mimeType, data: b64 } },
          ],
        },
      ],
    };

    let response: Response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (error) {
      throw new Error(
        `Gemini: сеть недоступна или ключ ограничен политиками браузера (${
          normalizePlatformError(error).message
        })`,
      );
    }

    if (!response.ok) {
      if (response.status === 503) {
        throw new Error(
          "Gemini сейчас перегружен (503 Service Unavailable). Подождите пару минут или смените модель на OpenRouter.",
        );
      }
      throw await parsePlatformError(response, "Gemini");
    }

    const data = await response.json();
    const text = data?.candidates?.[0]?.content?.parts?.[0]?.text;
    if (text) return { markdown: text };

    const finishReason = data?.candidates?.[0]?.finishReason;
    if (finishReason)
      throw new Error(`Gemini заблокировал ответ: ${finishReason}`);
    throw new Error("Пустой ответ от Gemini или неизвестный формат ответа.");
  }

  if (!key) throw new Error("API ключ не указан");
  if (activeContent.current) onProgress?.("Запрос к OpenRouter...");
  const payload = {
    model,
    messages: [
      {
        role: "user",
        content: [
          { type: "text", text: OCR_PROMPT },
          {
            type: "image_url",
            image_url: { url: `data:${mimeType};base64,${b64}` },
          },
        ],
      },
    ],
  };

  let response: Response;
  try {
    response = await fetch("https://openrouter.ai/api/v1/chat/completions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${key}`,
      },
      body: JSON.stringify(payload),
    });
  } catch (error) {
    throw new Error(
      `OpenRouter: сеть недоступна или запрос заблокирован (${normalizePlatformError(error).message})`,
    );
  }

  if (!response.ok) throw await parsePlatformError(response, "OpenRouter");

  const data = await response.json();
  const text = data?.choices?.[0]?.message?.content;
  if (text) return { markdown: text };
  throw new Error("Пустой ответ от OpenRouter или неизвестный формат ответа.");
}

export async function executeOllamaOcrForImage(
  b64: string,
  settings: { baseUrl: string; model: string },
  activeContent: { current: boolean },
  onProgress?: ProgressSink,
): Promise<OcrResult> {
  const url = buildOllamaGenerateUrl(settings.baseUrl);
  const model = normalizeOllamaModel(settings.model.trim());
  if (activeContent.current) onProgress?.(`Запрос к Ollama (${model})...`);

  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model,
        prompt: OCR_PROMPT,
        images: [b64],
        stream: false,
      }),
    });
  } catch (error) {
    throw new Error(
      `Ollama: сеть недоступна или CORS заблокировал запрос (${normalizePlatformError(error).message})`,
    );
  }

  if (!response.ok) throw await parsePlatformError(response, "Ollama");

  const data = await response.json();
  const text = data?.response;
  if (typeof text === "string" && text.trim()) return { markdown: text };
  throw new Error("Пустой ответ от Ollama или неизвестный формат ответа.");
}

export async function executeOllamaOcr(
  targetFile: File,
  settings: { baseUrl: string; model: string },
  activeContent: { current: boolean },
  onProgress: ProgressSink,
  onChunk?: (text: string, pageIdx?: number) => void,
  startPage = 1,
  onTotalPages?: (total: number) => void,
  pdfRenderScale?: number,
): Promise<OcrResult> {
  if (activeContent.current) onProgress("Подготовка файла...");

  if (targetFile.type === "application/pdf") {
    const md = await processPdfIntelligently(
      targetFile,
      (msg) => {
        if (activeContent.current) onProgress(msg);
      },
      async (b64) => {
        const res = await executeOllamaOcrForImage(
          b64,
          settings,
          activeContent,
          onProgress,
        );
        return res.markdown;
      },
      onChunk,
      startPage,
      onTotalPages,
      { renderScale: pdfRenderScale },
    );
    return { markdown: md };
  }

  const b64 = await imageFileToCroppedBase64(targetFile, cropWhiteBorders);
  return await executeOllamaOcrForImage(
    b64,
    settings,
    activeContent,
    onProgress,
  );
}

export async function executeLlmOcr(
  targetFile: File,
  settings: LlmSettings,
  activeContent: { current: boolean },
  onProgress: ProgressSink,
  onChunk?: (text: string, pageIdx?: number) => void,
  startPage = 1,
  onTotalPages?: (total: number) => void,
  pdfRenderScale?: number,
): Promise<OcrResult> {
  if (activeContent.current) onProgress("Подготовка файла...");

  if (targetFile.type === "application/pdf") {
    const md = await processPdfIntelligently(
      targetFile,
      (msg) => {
        if (activeContent.current) onProgress(msg);
      },
      async (b64) => {
        const res = await executeLlmOcrForImage(
          b64,
          "image/jpeg",
          settings,
          activeContent,
          onProgress,
        );
        return res.markdown;
      },
      onChunk,
      startPage,
      onTotalPages,
      { renderScale: pdfRenderScale },
    );
    return { markdown: md };
  }

  const b64 = await imageFileToCroppedBase64(targetFile, cropWhiteBorders);
  const result = await executeLlmOcrForImage(
    b64,
    "image/jpeg",
    settings,
    activeContent,
    onProgress,
  );
  onChunk?.(result.markdown);
  return result;
}
