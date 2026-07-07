#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/debug/build-reference-screenshots.sh [options]

Defaults:
  - fixtures: debug/fixtures
  - references: debug/reference
  - output: debug/artifacts/reference-screenshots
  - mode: one tall PNG screenshot per PDF, with a matching .md reference

Options:
  --fixture GLOB             Select PDF fixture basenames; may be repeated.
  --fixtures DIR             Fixture directory; default debug/fixtures.
  --expected-root DIR        Reference directory; default debug/reference.
  --output-root DIR          Artifact directory; default debug/artifacts/reference-screenshots.
  --max-pages N              First N pages to render into the tall screenshot; default 5.
  --dpi N                    PDF raster DPI; default 220.
  --gap N                    White gap between stacked pages; default 32.
  --format CSV               png, jpg, or comma-separated values; default png.
  --publish-fixtures         Also copy generated screenshots into debug/fixtures
                             and generated references into debug/reference.
  --fixture-root DIR         Published fixture directory; default debug/fixtures.
EOF
}

fixtures_root="debug/fixtures"
expected_root="debug/reference"
output_root="debug/artifacts/reference-screenshots"
max_pages=5
dpi=220
gap=32
formats="png"
publish_fixtures=0
fixture_root="debug/fixtures"
fixture_patterns=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fixtures)
      fixtures_root="$2"
      shift 2
      ;;
    --expected-root)
      expected_root="$2"
      shift 2
      ;;
    --output-root)
      output_root="$2"
      shift 2
      ;;
    --fixture)
      fixture_patterns+=("$2")
      shift 2
      ;;
    --max-pages)
      max_pages="$2"
      shift 2
      ;;
    --dpi)
      dpi="$2"
      shift 2
      ;;
    --gap)
      gap="$2"
      shift 2
      ;;
    --format)
      formats="$2"
      shift 2
      ;;
    --publish-fixtures)
      publish_fixtures=1
      shift
      ;;
    --fixture-root)
      fixture_root="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! "$max_pages" =~ ^[1-9][0-9]*$ ]]; then
  echo "--max-pages must be a positive integer" >&2
  exit 2
fi
if [[ ! "$dpi" =~ ^[1-9][0-9]*$ ]]; then
  echo "--dpi must be a positive integer" >&2
  exit 2
fi
if [[ ! "$gap" =~ ^[0-9]+$ ]]; then
  echo "--gap must be a non-negative integer" >&2
  exit 2
fi

fixtures_root="$(realpath "$fixtures_root")"
expected_root="$(realpath "$expected_root")"
mkdir -p "$output_root"
output_root="$(realpath "$output_root")"
images_root="$output_root/images"
references_root="$output_root/reference"
manifest="$output_root/manifest.md"
rm -rf "$images_root" "$references_root"
mkdir -p "$images_root" "$references_root"
if [[ "$publish_fixtures" -eq 1 ]]; then
  mkdir -p "$fixture_root" "$expected_root"
  fixture_root="$(realpath "$fixture_root")"
fi

matches_fixture_patterns() {
  local file_name="$1"
  local pattern
  if [[ ${#fixture_patterns[@]} -eq 0 ]]; then
    return 0
  fi
  for pattern in "${fixture_patterns[@]}"; do
    if [[ "$file_name" == $pattern ]]; then
      return 0
    fi
  done
  return 1
}

selected_pdfs=()
skipped_pdfs=()
while IFS= read -r fixture; do
  file_name="$(basename "$fixture")"
  if ! matches_fixture_patterns "$file_name"; then
    continue
  fi
  if [[ -f "$expected_root/$file_name.md" ]]; then
    selected_pdfs+=("$fixture")
  else
    skipped_pdfs+=("$file_name")
  fi
done < <(
  find "$fixtures_root" -maxdepth 1 -type f -iname '*.pdf' -printf '%p\n' | sort
)

if [[ ${#selected_pdfs[@]} -eq 0 ]]; then
  echo "No PDF fixtures with matching references were selected." >&2
  exit 2
fi

mapfile -t outputs < <(
  python3 scripts/debug/debug_pdf_image_probe.py \
    "${selected_pdfs[@]}" \
    --expected-root "$expected_root" \
    --output-dir "$images_root" \
    --probe-reference-root "$references_root" \
    --max-pages "$max_pages" \
    --dpi "$dpi" \
    --gap "$gap" \
    --stack-pages \
    --format "$formats"
)

published_count=0
if [[ "$publish_fixtures" -eq 1 ]]; then
  for output in "${outputs[@]}"; do
    output_name="$(basename "$output")"
    reference_name="$output_name.md"
    cp "$output" "$fixture_root/$output_name"
    cp "$references_root/$reference_name" "$expected_root/$reference_name"
    published_count=$((published_count + 1))
  done
fi

{
  printf '# Reference Screenshots\n\n'
  printf -- '- fixtures: `%s`\n' "$fixtures_root"
  printf -- '- references: `%s`\n' "$expected_root"
  printf -- '- max pages per screenshot: `%s`\n' "$max_pages"
  printf -- '- DPI: `%s`\n' "$dpi"
  printf -- '- formats: `%s`\n' "$formats"
  printf -- '- generated screenshots: `%s`\n' "${#outputs[@]}"
  printf -- '- published fixtures: `%s`\n' "$published_count"
  if [[ "$publish_fixtures" -eq 1 ]]; then
    printf -- '- fixture root: `%s`\n' "$fixture_root"
  fi
  printf '\n'
  printf '| Source PDF | Long Screenshot | Reference Markdown |\n'
  printf '| --- | --- | --- |\n'
  for output in "${outputs[@]}"; do
    output_name="$(basename "$output")"
    source_name="${output_name%.raster.png}"
    source_name="${source_name%.raster.jpg}"
    printf '| `%s` | `%s` | `%s` |\n' \
      "$source_name" \
      "images/$output_name" \
      "reference/$output_name.md"
  done
  if [[ ${#skipped_pdfs[@]} -gt 0 ]]; then
    printf '\n## Skipped\n\n'
    for file_name in "${skipped_pdfs[@]}"; do
      printf -- '- `%s`: missing `%s.md`\n' "$file_name" "$file_name"
    done
  fi
} >"$manifest"

printf 'Reference screenshots complete: %s\n' "$output_root"
