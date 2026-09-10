#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
manifest="$repo_root/pipeline-core/Cargo.toml"
library="$repo_root/pipeline-core/target/release/libittm_pipeline_core.so"

cargo test --locked --manifest-path "$manifest"
cargo build --locked --release --manifest-path "$manifest"
python3 "$repo_root/scripts/ci/verify-pipeline-core-parity.py" "$library"

printf '%s\n' "$library"
