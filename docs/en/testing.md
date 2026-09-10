# Test responsibilities

[Documentation](./README.md) | [Pipeline diagnostics](../ru/pipeline/README.md)

Use the smallest gate that owns the changed boundary, then run the complete PR
job for that runtime. The workflow source is `.github/workflows/tests.yml`.

## Pull-request gates

| Gate or command                                                               | Responsibility                                                             |
| ----------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `npm run format:check`                                                        | Repository formatting only                                                 |
| `npx eslint .`                                                                | JavaScript/TypeScript static checks                                        |
| `npm run typecheck`                                                           | Web, gateway and edge TypeScript contracts                                 |
| `npm run test:pipeline-docs`                                                  | Backend profile names and five public override keys/modes in documentation |
| `npm test`                                                                    | Web, gateway and edge unit/contract tests matching `*.test.ts`             |
| `npm run build`                                                               | Production web and bundled gateway                                         |
| `npm run model:browser-reviewer && npm run build:pages && npm run test:pages` | Lite Pages base path, workers, pinned model and WASM assets                |
| `npm run test:contract`                                                       | Task worker/input plus web/Python layout and upload boundary               |
| `npm run test:smoke`                                                          | Gateway routes/static files and FastAPI route smoke                        |
| Python fast suites                                                            | API, engines, layout, shared pipeline and debug-script regressions         |
| `docker compose config --quiet`                                               | Compose syntax and service wiring                                          |
| `npm run test:sast`                                                           | First-party security patterns in the configured source targets             |

The Python fast command used by CI is:

```bash
docker run --rm \
  -v "$PWD/scripts:/scripts:ro" \
  -v "$PWD/debug:/debug:ro" \
  -v "$PWD/LLM-OCR:/LLM-OCR:ro" \
  ittm-ocr-ci \
  python -m pytest \
    tests/api tests/engines tests/layout tests/pipeline tests/debug -q
```

It intentionally does not include `tests/quality`, `tests/recognition` or
`tests/sparse_pipeline`; choose those explicitly when their code changes.

### Shared Rust route

The Node job explicitly installs Rust 1.96.1 and the WASM target, matching the
pinned OCR builder. Both production builds run `pipeline-core` tests. The native
and WASM ABI verifiers exercise block planning, OCR word handoff, segment assembly
and all eight completed stage flags.

`tests/pipeline/test_service_rust_route.py` checks the Python service boundary:
page dimensions and profile options must preserve Rust's block requests, crop
pixels, stage flags and final text. The service must propagate native errors.
These replace the retired orchestration assertions that required Python page
splitting, full-page OCR fallbacks or post-processing after Rust assembly.
Standalone Python repair helpers retain their own tests.

The small-raster Rust regression distinguishes valid pages with no OCR jobs from
invalid input. ABI text fixtures use thick text bands; thin horizontal lines can
correctly be classified as rules and produce no text jobs.

Literal sample transcripts live in `ocr/tests/data/reference`, are copied into
the test image, and must not be reformatted. Their original hash and quality
threshold checks remain part of the corpus tests.

## Python suites

| Suite                   | Responsibility                                                       |
| ----------------------- | -------------------------------------------------------------------- |
| `tests/api`             | FastAPI routes, bounded uploads, PDF modes and progress              |
| `tests/engines`         | Engine selection, preprocessing and backend profile flags            |
| `tests/layout`          | Current public layout, tables, sparse codes and Markdown formatting  |
| `tests/pipeline`        | Shared `pipeline-core` adapters, flags and stage contracts           |
| `tests/debug`           | Public debug runners and report generation                           |
| `tests/recognition`     | Candidate, language, identifier, span and token lattices             |
| `tests/sparse_pipeline` | Diagnostic sparse stages, invariants, limits and artifact contracts  |
| `tests/quality`         | Generated corpus, mutation, resource and real OCR quality thresholds |
| `tests/support`         | Fixture generators and metric helpers; not an independent gate       |

Focused example:

```bash
cd ocr
python -m pytest tests/sparse_pipeline tests/recognition -q
```

## Scheduled and resource gates

| Command or workflow        | Responsibility                                                        |
| -------------------------- | --------------------------------------------------------------------- |
| `npm run test:ocr:browser` | Real Tesseract.js multilingual browser OCR                            |
| `npm run test:ocr:quality` | Backend generated quality matrix in the OCR CI image                  |
| `npm run test:resources`   | Generated fuzz under Docker memory/CPU/PID limits                     |
| `npm run test:compose`     | Built Compose ingress → gateway → OCR smoke                           |
| `npm run test:sca`         | npm audit, Trivy source/images, SBOM and accepted-risk reconciliation |

Generated binary fixtures live under ignored `ocr/tests/fixtures`. Debug PNGs
are evidence, not an oracle. A quality claim requires a matching reference and
the quality metric named by the test.

`npm run test:ocr:browser` requires local `eng`, `rus` and `chi_sim`
traineddata. Set `BROWSER_OCR_LANG_PATH` when the files are not installed in a
standard Tesseract directory.
