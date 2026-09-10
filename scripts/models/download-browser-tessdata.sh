#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
target_root="${BROWSER_OCR_LANG_PATH:-$repo_root/.cache/tessdata}"
commit="87416418657359cb625c412a48b6e1d6d41c29bd"
mkdir -p "$target_root"

declare -A hashes=(
  [eng]="7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"
  [rus]="e16e5e036cce1d9ec2b00063cf8b54472625b9e14d893a169e2b0dedeb4df225"
  [chi_sim]="a5fcb6f0db1e1d6d8522f39db4e848f05984669172e584e8d76b6b3141e1f730"
  [ell]="4fba8a0b461038d51f1c20d043d4f2ac38c4e778f1b90830847f7bd8fa3ba726"
  [equ]="8f660323d8a7b7a0e8d2fae1a3439e6e470222bfbb990b2ab7fe9e1fb4791c0b"
)

for language in eng rus chi_sim ell equ; do
  destination="$target_root/$language.traineddata"
  expected="${hashes[$language]}"
  if [[ -f "$destination" ]] &&
    printf '%s  %s\n' "$expected" "$destination" | sha256sum --check --status; then
    continue
  fi

  temporary="$destination.tmp.$$"
  trap 'rm -f "$temporary"' EXIT
  raw_url="https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/$commit/$language.traineddata"
  github_url="https://github.com/tesseract-ocr/tessdata_fast/raw/$commit/$language.traineddata"
  if ! curl --fail --location --retry 5 --retry-all-errors \
    --connect-timeout 15 --max-time 300 --output "$temporary" "$raw_url"; then
    curl --fail --location --retry 5 --retry-all-errors \
      --connect-timeout 15 --max-time 300 --output "$temporary" "$github_url"
  fi
  printf '%s  %s\n' "$expected" "$temporary" | sha256sum --check --status
  mv "$temporary" "$destination"
  trap - EXIT
done

printf 'browser tessdata_fast ready: %s\n' "$target_root"
