# Native OCR runtime

`ittm-ocr` owns the native Tesseract adapter, document loading, PDF text-layer
selection, page iteration, HTTP transport and diagnostic CLI. Structural
decisions still come from the same `pipeline-core` used by WASM.

The Tesseract adapter reuses initialized language handles within one document,
passes RGB pixels directly through the C API, and submits word boxes and
confidence back to the shared stage engine. Missing optional languages produce
empty candidates. Recognition has a deadline and supports cancellation.

## Build and run

Install Rust, pkg-config, Tesseract development headers, English/Russian
traineddata and Poppler. Debian/Ubuntu packages are `pkg-config`,
`libtesseract-dev`, `libleptonica-dev`, `tesseract-ocr-eng`, `tesseract-ocr-rus`
and `poppler-utils`.

```bash
npm run build:ocr-runtime
ocr-runtime/target/release/ittm-ocr check
ocr-runtime/target/release/ittm-ocr serve --port 8000
ocr-runtime/target/release/ittm-ocr convert document.pdf --output document.md
ocr-runtime/target/release/ittm-ocr convert image.png --stream
```

`bash scripts/runtime/run-local.sh` and the default Docker Compose configuration
start the Rust service. Local launch chooses free ports when no ports are given.

The HTTP service preserves `/convert`, `/convert/stream`, their `/v1` aliases,
the profile/flag catalog and the page/progress/complete/error NDJSON protocol.
Uploads are spooled to a temporary file. `OCR_CONCURRENCY` bounds active requests
(default 1); excess requests receive 429. Pixel/page/upload limits retain the
existing `OCR_MAX_*` environment names. `OCR_COMMAND_TIMEOUT_SECONDS` defaults
to 60 seconds. `OCR_CORS_ORIGINS` accepts explicit origins.

## Optional Python engine and compatibility service

EasyOCR remains a persistent Python worker that exchanges RGB pixels and word
evidence with Rust. Install EasyOCR and its model files in a dedicated Python
environment and configure `ITTM_EASYOCR_PYTHON`; `ITTM_EASYOCR_WORKER` can
override the path to `ocr/app/native_worker.py`. Select `--engine easyocr` to use
it. Models are not downloaded automatically by this worker. The default Rust
Docker image contains no Python interpreter or EasyOCR models.

`auto` currently uses the shared Tesseract language route. The native service
does not install EasyOCR on demand; `/install-easyocr` explains the required
worker configuration. The previous Python service remains available for its
installation UI, optional providers and reference diagnostics:

```bash
OCR_RUNTIME=python bash scripts/runtime/run-local.sh
docker compose -f docker-compose.yml -f docker-compose.python.yml up -d --build
```

## Diagnostic checkpoints

```bash
npm run debug:native -- image.png --output /tmp/ocr-debug
npm run debug:native -- image.png --output /tmp/ocr-plan --plan-only
npm run debug:native -- image.png --output /tmp/ocr-replay --resume /tmp/ocr-debug/checkpoint.json
```

Checkpoints contain the source image, exact OCR attempts, profiles and ABI/route
identifiers. Resume reruns deterministic planning and reuses recorded OCR
evidence; every request is checked against the recorded fields. Source pixels
must match. Each completed attempt is saved atomically. This is a native replay
tool; the existing Python stage-injection labs retain their separate formats.

## Validation

```bash
cargo test --locked --manifest-path pipeline-core/Cargo.toml
npm run test:ocr-runtime
python3 scripts/ci/verify-native-profiles.py
python3 scripts/ci/test-native-runtime.py
```

The last command needs the Python comparison dependencies from
`ocr/requirements-ci.txt`, a release build of both crates, English/Russian
traineddata and DejaVu Sans. It compares text, transparency, tables and trusted
PDFs with the existing Python route, exercises PDF raster fallback and checkpoint
replay, then starts a local HTTP server to check multipart and streaming output.

The profile catalog is committed data. Update it after changing Python profiles
with `python3 scripts/ci/verify-native-profiles.py --write`.
