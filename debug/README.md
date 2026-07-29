# Debug OCR

This directory is a local OCR debugging workspace.

Tracked files:

- `fixtures/` - two tracked SAMPLE inputs plus ignored local real fixtures.
- `reference/*.md` - manual reference text paired by filename.
- `fixtures/.gitkeep` and `reference/.gitkeep` - empty directory anchors.
- `private-report.md` - local investigation report.
- `.env.sample` - API-engine placeholders.

Ignored files:

- Real OCR inputs copied into `debug/fixtures/`, except the two tracked SAMPLE files.
- `result.csv` and `time.csv` - final per-method matrices; they are runtime
  output written by `scripts/debug/debug_matrix_report.py` and are regenerated on
  every `scripts/debug/debug-all.sh` run, so they are not part of the repository.
- The whole `debug/tmp/` runtime tree. It is not part of the repository and
  `scripts/debug/debug-all.sh` recreates it, including engine subdirectories, when
  it does not exist.
- Local `debug/.env`.

## Clean-room sparse stages

The rewritten pipeline keeps each stage independently replayable. Stage 6
consumes only the immutable geometry result from stage 1; it does not invoke an
OCR engine and writes text-only object evidence below `06-objects/`.

Run a queued process-parallel replay over the known PNG corpus:

```bash
python scripts/debug/debug_object_reconstruction.py \
  --input debug/generated/stage1-line-owner-full1232-20260719 \
  --output debug/tmp/object-corpus \
  --run-id stage6-full1232 \
  --workers 4 \
  --executor process \
  --fail-on-degraded
```

The fresh run directory contains `summary.json`, `summary.tsv`, `summary.md`
and one atomic `items/<source-id>/06-objects/` directory per image. A successful
run proves exact segment ownership. `UNKNOWN` is intentional when image-free
geometry cannot distinguish a paragraph from a list; later recognition and
grammar stages may refine it without inventing evidence.

Stage 7 has an integrated bounded runner for the exact rewrite order
`3 -> 1 -> 6 -> 4 -> 5 -> 2 -> 7`:

```bash
python3 scripts/debug/debug_document_assembly.py \
  --input debug/generated/stage1-known-strict-20260719-v2 \
  --run-id stage7-known-24 \
  --limit 24 \
  --engines tesseract \
  --prepare-workers 4 \
  --ocr-page-workers 2
```

Page preparation uses a bounded process pool while several persistent OCR
sessions consume prepared pages concurrently. Every item gets an atomic
`07-document/` evidence tree; corpus summaries are JSON, TSV and Markdown.
Known text is loaded only after artifact publication and is scored with exact
Levenshtein loss after removing Unicode whitespace only. See
[`docs/ru/sparse-rewrite-stage7.md`](../docs/ru/sparse-rewrite-stage7.md).

The v20 unit + real failure-corpus gate checks every command and sends a desktop
notification on both success and failure. Run `debug-all` first so the ignored
real raster fixtures exist, then use one immutable run ID:

```bash
scripts/debug/debug-all.sh
RUN_ID=v20-full-$(date +%Y%m%d-%H%M%S) \
scripts/debug/run-sparse-v20.sh
```

The default acceptance set must include `000041…raster.png`,
`09.03.03…raster.png`, and `Adobe Scan Jun 20…raster.png`; it cannot silently
fall back to SAMPLE. `V20_TUTORIAL_SET=all` writes the same object-local debug
tree for every `debug/fixtures/*.raster.png`. SAMPLE is available only through
the explicit `V20_TUTORIAL_SET=sample` smoke mode and does not certify v20.
`V20_EVIDENCE_ONLY_SOURCES` is a colon-separated list of exact input-relative
labels or basenames; the final gate replays that actual list against every
summary item instead of assuming a hard-coded filename. Set it to an empty
value to disable exclusions. Recursive inputs with
equal basenames remain distinct because tutorial item IDs hash their normalized
paths. `V20_ARTIFACT_OUTPUT` and `V20_ROOT_REPORT` may point outside the repo;
external artifact paths are stored as absolute paths and report links stay
relative to the report that contains them.

The known-text corpus is the page-level baseline and uses
`V20_SINGLE_CONTEXT_PSM` (default `6`). The tutorial uses that same value for a
single block and switches multi-block/table objects to
`V20_DOCUMENT_CONTEXT_PSM` (default `4`). Both effective profiles are recorded
in provenance; this context routing is intentional, so comparisons must not
present the two gates as one identical Tesseract configuration.

Every page below `debug/artifacts/v20/<run-id>/items/` contains:

- exact sparse matrix JSON/TSV/text plus a logical matrix PNG and literal Stage
  1 ownership PNG;
- `objects/<object-id>/object.md`, `object.txt`, `object.json` and `debag.md`;
- object-local `segments/` with literal raw/ownership PNG, OCR text and metadata;
- object-local `blocks/<block-id>/` with literal raw/enhanced PNG, membership,
  OR/XOR provenance, OCR text and OCR metadata;
- seven stage logs, final `document.md`/`document.txt`, and `provenance.json`
  containing commit, argv/config and input SHA-256.

Failed Stage 7 quality gates still preserve these artifacts before the wrapper
returns non-zero. Earlier unit-stage failures remain fail-fast.

Use the explicit full run when all generated references are required:

```bash
RUN_ID=v20-full1232-$(date +%Y%m%d-%H%M%S) \
V20_INPUT=debug/generated/stage1-line-owner-full1232-20260719 \
V20_LIMIT=1232 \
V20_TUTORIAL_SET=all \
scripts/debug/run-sparse-v20.sh
```

## Run Everything

Default run: all local non-API engines, automatic flags, all fixtures from
`debug/fixtures/`.
Selected PDF fixtures are also rasterized into PNG and JPEG image fixtures by
default, so `*.pdf.raster.png` and `*.pdf.raster.jpg` appear as separate rows in
`result.csv` and `time.csv`.

```bash
scripts/debug/debug-all.sh
```

This writes:

```text
debug/result.csv
debug/time.csv
debug/tmp/tesseract/
debug/tmp/easyocr/
debug/tmp/browser-tesseract/
```

PDF raster rows are the image-path acceptance check. Curriculum PDFs are
rendered by the debug runner into separate 300 DPI PNG/JPEG fixtures and must
reach the 90% gate without relying on the PDF text layer. Release/API requests
still default to `pdf_mode=auto`; use the public `pdf_mode=raster` query or CLI
flag only when the caller explicitly wants to force page OCR. The CSV files are
still written before the quality gate reports any failure.

Tracked samples include:

- `fixtures/SAMPLE_4k.png` - 3840x2160 edge-to-edge SAMPLE text;
  default Tesseract debug recognition must stay above 90%.
- `fixtures/SAMPLE_mixed_ru_en_zh_table_image.pdf` - image-only PDF
  with a hard 10x14 mixed Russian/English/Chinese table, merged subsection
  rows, digit/letter/`й` identifiers, and Markdown placeholder-cell coverage.

API folders are created under `debug/tmp/` at runtime, but remain empty until
API runners are implemented:

```text
debug/tmp/api-ollama/
debug/tmp/api-openrouter/
debug/tmp/api-gemini/
```

Selecting an API engine fails with a clear error instead of silently producing
fake OCR:

```bash
scripts/debug/debug-all.sh --engines api-ollama
```

## Run One File

```bash
scripts/debug/debug-all.sh --fixture 'image (6).png'
```

`--fixture` accepts shell globs matched against names under
`debug/fixtures/` and may be repeated. Manual expected Markdown is
looked up under `debug/reference/` using the same basename plus
`.md`.

Limit backend PDF pages:

```bash
scripts/debug/debug-all.sh --fixture 'Adobe Scan Oct 26, 2022 (1).pdf' --max-pages 5
```

Use a per-file rule when running a mixed set:

```bash
scripts/debug/debug-all.sh --fixture-max-pages 'Adobe Scan Oct 26, 2022 (1).pdf=5'
```

Disable PDF raster rows for a faster PDF-only backend check:

```bash
scripts/debug/debug-all.sh --fixture '*.pdf' --no-pdf-raster
```

Change raster formats or the page limit:

```bash
scripts/debug/debug-all.sh \
  --fixture '*.pdf' \
  --pdf-raster-formats png,jpg \
  --pdf-raster-max-pages 5
```

## Select Engines

All non-API engines are used by default:

```bash
scripts/debug/debug-all.sh --engines tesseract,easyocr,browser-tesseract
```

One backend engine:

```bash
scripts/debug/debug-all.sh --engines tesseract
```

Backend without browser:

```bash
scripts/debug/debug-all.sh --engines tesseract,easyocr
```

Browser only is supported for image fixtures:

```bash
scripts/debug/debug-all.sh --engines browser-tesseract --fixture '*.png'
```

## Select Flags

By default every backend engine uses its automatic profile.

Use one backend profile for every backend engine:

```bash
scripts/debug/debug-all.sh \
  --engines tesseract,easyocr \
  --pipeline-profile backend_plain_text
```

Override flags for only one backend engine:

```bash
scripts/debug/debug-all.sh \
  --engine-profile tesseract=backend_tesseract_standard \
  --engine-profile easyocr=backend_easyocr_table
```

Select the browser OCR profile:

```bash
scripts/debug/debug-all.sh --browser-profile browser_tesseract_dewarp
```

The browser benchmark uses a Node Canvas shim for the same resize, dewarp,
edge-word, sparse-cover, and dense-grid paths used by the UI. Dense curriculum
tables run overlapping PSM passes, so the default browser timeout is 900s.

## Flag Sweep

Normal result files are CSV-only. XLSX is reserved for flag selection reports.

```bash
python3 scripts/debug/debug_flag_sweep.py \
  'debug/fixtures/image (7).png' \
  --output debug/tmp/flag-sweep-image7.csv \
  --xlsx-output debug/tmp/flag-sweep-image7.xlsx \
  --scale 1 --scale 2 --scale 3 \
  --preprocess rgb --preprocess autocontrast \
  --psm 3 --psm 4 --psm 6 --psm 11 --psm 12
```

The best row per file is highlighted in yellow in the XLSX report.

Recognized table snapshots are written as one
`debug/tmp/tables/<method>/<fixture>.tables.md` file per result. All table
blocks are combined in that file instead of creating hundreds of tiny files.
They use Markdown `|` separators and preserve empty placeholder cells; table
snapshots are not comma-separated CSV.

## PDF As Images

The default `scripts/debug/debug-all.sh` run already adds selected PDF fixtures as
PNG and JPEG image rows. Use the lower-level probe only when you need to create
those image fixtures without running the full matrix:

```bash
scripts/debug/debug_pdf_image_probe.py \
  'debug/fixtures/09.03.03_05(ИУ1).pdf' \
  --max-pages 5 \
  --format png,jpg
scripts/debug/run-debug.sh \
  --fixtures debug/tmp/pdf-image-fixtures \
  --expected-root debug/tmp/pdf-image-reference \
  --output debug/tmp/pdf-image-probe-run \
  --engines tesseract \
  --fixture '09.03.03_05(ИУ1).pdf.raster.png' \
  --timeout 420 \
  --gpu auto
```

Generated files stay under `debug/tmp/`. The probe limits copied expected text
to the same first pages that were rendered.

## API Environment

Copy `.env.sample` to `.env` for local API experiments:

```bash
cp debug/.env.sample debug/.env
```

Ollama defaults to local host:

```text
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=
```

API engines are not part of the default run.
