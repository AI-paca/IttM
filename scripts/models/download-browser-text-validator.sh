#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
model_id="HuggingFaceTB/SmolLM2-135M-Instruct"
revision="83212e1e2b3cfd6958f3707877bb878945dea8ee"
target_dir="${repo_root}/.models/browser/${model_id}"
base_url="https://huggingface.co/${model_id}/resolve/${revision}"

declare -A expected_sizes=(
  ["config.json"]=861
  ["generation_config.json"]=132
  ["merges.txt"]=466391
  ["onnx/model_quantized.onnx"]=137147981
  ["special_tokens_map.json"]=655
  ["tokenizer.json"]=2104556
  ["tokenizer_config.json"]=3764
  ["vocab.json"]=800662
)

download_file() {
  local relative_path="$1"
  local expected_size="${expected_sizes[$relative_path]}"
  local destination="${target_dir}/${relative_path}"
  local temporary="${destination}.part"

  mkdir -p "$(dirname "$destination")"
  if [[ -f "$destination" ]] && [[ "$(stat -c '%s' "$destination")" == "$expected_size" ]]; then
    printf 'ok %s\n' "$relative_path"
    return
  fi

  rm -f "$temporary"
  curl \
    --fail \
    --location \
    --retry 5 \
    --retry-all-errors \
    --continue-at - \
    --output "$temporary" \
    "${base_url}/${relative_path}"

  local actual_size
  actual_size="$(stat -c '%s' "$temporary")"
  if [[ "$actual_size" != "$expected_size" ]]; then
    printf 'size mismatch for %s: expected %s, got %s\n' \
      "$relative_path" "$expected_size" "$actual_size" >&2
    exit 1
  fi
  mv "$temporary" "$destination"
}

for relative_path in \
  config.json \
  generation_config.json \
  merges.txt \
  onnx/model_quantized.onnx \
  special_tokens_map.json \
  tokenizer.json \
  tokenizer_config.json \
  vocab.json
do
  download_file "$relative_path"
done

printf 'browser text validator ready: %s\n' "$target_dir"
