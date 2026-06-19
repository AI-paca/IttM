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
