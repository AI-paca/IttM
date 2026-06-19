# Security Policy

[Русский](../ru/security.md) | [Documentation](./README.md)

## Trust Boundaries

| Mode                      | Document destination                        | Trust level           |
| ------------------------- | ------------------------------------------- | --------------------- |
| Browser OCR               | Tab memory and browser workers only         | Local device          |
| Local Tesseract / EasyOCR | Local nginx, gateway, and Python OCR        | Trusted local runtime |
| External LLM              | Gemini, OpenRouter, or another selected API | Third-party provider  |

## Local Processing

- Docker publishes only nginx. The gateway and OCR service remain inside the
  Compose network.
- Local modes send the original `File` as multipart. The frontend does not
  convert it to Base64 or call `arrayBuffer()` before upload.
- The gateway streams the request body and does not store documents in a
  database.
- Python reads the upload in chunks but assembles it into a complete `bytes`
  object before OCR.
- Images are processed from memory. PDFs are temporarily written under
  `tempfile` because Poppler requires a path; the directory is removed after
  the request.

The project does not claim absolute operating-system zero retention. Temporary
PDF files, swap, crash dumps, and host logs depend on deployment settings.

## Browser OCR

Browser mode does not send the source document to the backend. Tesseract.js,
PDF.js, and preprocessing use browser workers where supported. A complete PDF
still enters a worker `ArrayBuffer`, which remains a memory risk for very large
files.

## External LLMs

- Gemini/OpenRouter require explicit consent for the current session.
- The document may be resized, Base64-encoded, and sent to the provider.
- Provider retention and processing policies apply after upload.
- Provider API keys stay in frontend state and are not sent to local OCR.

## Open Risks

- The local API has no authentication and relies on loopback/network binding.
- The in-memory task queue (`maxWorkers: 1`, `maxQueued: 32`) provides a task ID
  and server-side cancel (`POST /api/tasks/:id/cancel`), but does not survive a
  restart: durable queue, retry, and retention are absent.
- The complete upload exists in Python memory.
- Not every decompression/image bomb pattern is rejected.
- Streaming failures after headers are represented as NDJSON `error` events
  inside HTTP 200.
