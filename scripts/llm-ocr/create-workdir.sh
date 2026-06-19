#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
workdir="$repo_root/LLM-OCR"

mkdir -p "$workdir"
mkdir -p "${HF_HOME:-$HOME/.cache/huggingface}"

if [[ ! -f "$workdir/.env" ]]; then
  cp "$workdir/.env.example" "$workdir/.env"
fi

if [[ ! -f "$workdir/.evn" ]]; then
  cp "$workdir/.evn.example" "$workdir/.evn"
fi

ensure_key() {
  local file="$1"
  local key="$2"
  local value="$3"
  if ! grep -q "^${key}=" "$file"; then
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

for env_file in "$workdir/.env" "$workdir/.evn"; do
  ensure_key "$env_file" "HF_HOME" '${HOME}/.cache/huggingface'
  ensure_key "$env_file" "NEMOTRON_OCR_REPO_DIR" '${HOME}/.cache/huggingface/nemotron-ocr-v2-src'
  ensure_key "$env_file" "OLLAMA_HOST" "http://127.0.0.1:11434"
  ensure_key "$env_file" "LLM_OCR_API_HOST" "127.0.0.1"
  ensure_key "$env_file" "LLM_OCR_API_PORT" "18080"
done

echo "LLM-OCR workdir: $workdir"
echo "Token files: $workdir/.env and $workdir/.evn"
echo "Model cache: ${HF_HOME:-$HOME/.cache/huggingface}"
echo "Run: scripts/llm-ocr/run-local-api.sh --list"
