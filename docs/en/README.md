# IttM Documentation

[Root README](../../README.md) | [Русский](../ru/README.md)

IttM is a gateway-first OCR tool: the Extraction contract (gateway API) is the
core, and the Web UI, CLI wrapper, and `curl` are equal clients over the same
backend. How you run the project (Docker Compose, bare-metal, static build for
GitHub Pages) and how you access it (Web UI or `curl`) are **not** processing
modes. Four engines do the processing; access to them is uniform for all clients.

| Engine          | Where it runs                  | Source document transfer                          |
| --------------- | ------------------------------ | ------------------------------------------------- |
| Local Tesseract | Python FastAPI (backend)       | multipart, no browser-side `arrayBuffer()`/Base64 |
| Local EasyOCR   | Python FastAPI (backend)       | multipart, no browser-side `arrayBuffer()`/Base64 |
| Browser OCR     | Tesseract.js worker in browser | never leaves the tab                              |
| External LLM    | selected provider API          | only after explicit user consent                  |

## User Documentation

- [Security policy](./security.md)
- [Testing](./testing.md)
- [SBOM / dependencies](./sbom-report.md)

## Developer Documentation

- [Engine hardening history](./engine-hardening-progress.md)
- [Tesseract quality experiment](./experiments/tesseract-quality.md)

## Russian-only documentation

The following documents exist only in Russian. They cover architecture details,
Docker/manual launch, course plans, grading notes, and the development roadmap
history:

- [Архитектура](../ru/architecture.md)
- [Ограничения OCR-архитектуры](../ru/architecture-limitations.md)
- [Debug](../ru/debug.md)
- [Движок и профили](../ru/engine/README.md)
- [Ручной запуск Docker](../ru/docker-manual-launch.md)
- [Границы ответственности](../ru/course/boundaries.md)
- [История roadmap](../ru/roadmap/history.md)
- [Критерии заданий курса](../ru/course/course_tasks.md)

Known gaps relative to the ideal target architecture are tracked in the
root-level [`draft-to-do.md`](../../draft-to-do.md).
