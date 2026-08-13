# Security policy

[Русский](../ru/security.md) | [Documentation](./README.md)

## Trust boundaries

| Mode                      | Document destination                 |
| ------------------------- | ------------------------------------ |
| Browser OCR               | Tab memory and browser workers       |
| Local Tesseract / EasyOCR | Local nginx, gateway, and Python OCR |
| Gemini / OpenRouter       | The selected third-party API         |
| Ollama                    | A user-configured local endpoint     |
| Edge proxy                | Configured origin or Gemini          |

Compose publishes nginx on `127.0.0.1` by default. Gateway and Python OCR stay
inside the Compose network. The local API has no authentication and must not be
exposed to an untrusted network without a separate authentication layer.

Local uploads are sent without browser-side Base64. The gateway does not write
Task API uploads to a database or object store, but it does buffer them as a
`File` and retains that `File` in the in-memory task record, including after a
terminal state. Python then assembles the accepted upload into one `bytes`
object. Images are processed in memory; PDFs use a temporary file for Poppler.
Swap, crash dumps, proxy logs, and host logs remain deployment concerns.

Browser OCR does not send the source file to the backend. A PDF is still held
in a worker `ArrayBuffer` and is subject to browser memory limits.

Gemini/OpenRouter requests require explicit UI consent. A directly supplied API
key remains in frontend state. An optional Edge worker may instead keep a
Gemini key in an environment secret. Provider retention policies apply after
upload.

Known limits include an unauthenticated local API, an in-memory non-durable task
queue without terminal-record eviction, unbounded direct-gateway buffering
before Python's upload limit, a complete Python upload copy, incomplete
decompression-bomb coverage, and streaming errors represented inside an
already-started HTTP 200 response.

Run first-party and dependency security checks with:

```bash
npm run test:sast
npm run test:sca
```

Start with the terminal finding or generated report. A known rule id, package,
and image are sufficient for triage; loading the complete Semgrep ruleset,
workflow, or report is unnecessary.
