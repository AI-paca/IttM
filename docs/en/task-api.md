# Task API

The code in `gateway/src/tasks` implements the public request record, in-memory
queue, event stream and Python OCR adapter behind `/api/tasks` and
`/api/extract/text`. Operator commands are in
[`docs/ru/pipeline/README.md`](../ru/pipeline/README.md).

## Routes

| Route                   | Method | Result                                    |
| ----------------------- | ------ | ----------------------------------------- |
| `/api/extract/text`     | POST   | Synchronous plain-text result             |
| `/api/tasks`            | POST   | Create a task, or wait when `sync` is set |
| `/api/tasks`            | GET    | Recent in-memory request records          |
| `/api/tasks/:id`        | GET    | Request, events, result and error         |
| `/api/tasks/:id/events` | GET    | SSE, or NDJSON when requested by `Accept` |
| `/api/tasks/:id/cancel` | POST   | Cancel a queued or running request        |

The queue states are `queued`, `running`, `cancelling`, `completed`, `failed`
and `cancelled`. Records disappear when the gateway process restarts.

## Request shape

`POST /api/tasks` accepts multipart, a binary body, or a JSON request record
defined in `gateway/src/tasks/types.ts`. Multipart and binary uploads are the
executable public inputs. JSON records are accepted, but the current local
executor can run only an in-memory `file` or `screenshot` source; other JSON
source kinds finish with `UNSUPPORTED_INPUT`. Query/header aliases are
normalized by `gateway/src/tasks/http-api.ts`:

- `engine` or `engine_type`: `auto`, `tesseract`, `easyocr`;
- `profile` or `pipeline_profile`: a backend profile name;
- `pdf_mode`, `X-PDF-Mode` or JSON `pdfMode`: `auto`, `raster`;
- `filename`: optional for a binary body; the gateway infers a supported media
  type from its signature when possible and otherwise uses `upload.bin`.

`sync=text|markdown|json|events` selects a synchronous response. The equivalent
`Accept` headers are also supported.

A serialized task intentionally exposes the normalized `request`, sequenced
`events`, terminal `result` or `error`, and timestamps. Uploaded file content is
never returned; a `file` source record contains only kind, name, size and media
type. Non-file JSON source records are serialized as supplied.

This serialization boundary is not a storage boundary. `http-api.ts` currently
uses `formData()` or `arrayBuffer()`, and the internal `TaskRecord` retains its
source `File` after completion. There is no record eviction. Compose nginx
limits incoming bodies to 32 MiB and Python later enforces
`OCR_MAX_UPLOAD_BYTES` (128 MiB by default), but direct Task API access has no
gateway-owned upload cap before buffering. `BoundedInputStorage` is tested code
but is not wired into `http-api.ts`.

## Failure evidence

Support should preserve:

- task `id`, `state`, `createdAt` and `updatedAt`;
- normalized `request.engine`, `request.profile`, `request.pdfMode` and source
  metadata;
- all event sequence numbers and warning/error codes;
- terminal `result.meta` or `error`, including `retryable` and `partial`;
- gateway and OCR logs for the same time window.

`gateway/src/tasks/task-service.ts` and `gateway/src/tasks/http-api.ts` are the
source of truth for serialization. `gateway/src/tasks/process-worker.ts` and
the OCR stream executor define cancellation and backend failure mapping.
