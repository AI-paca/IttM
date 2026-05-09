import { cropWhiteBorders, processPdfIntelligently } from "../lib/pdf-parser";
import { parsePlatformError, normalizePlatformError } from "./api-client";
import { imageFileToCroppedBase64 } from "./file-utils";
import type { LlmProvider, OcrResult, ProgressSink } from "./types";

const OCR_PROMPT =
  "Extract all text from this image/document. Output only the extracted text, nothing else, no markdown fences if not necessary.";

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
  if (!key) throw new Error("API ключ не указан");
  if (!model) throw new Error("Модель не указана");

  if (settings.provider === "gemini") {
    if (activeContent.current) onProgress?.("Запрос к Gemini...");
    const url = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(
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
