#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
manifest="$repo_root/rust/ocr-core/Cargo.toml"
target_dir="${CARGO_TARGET_DIR:-$repo_root/target/ocr-core}"
out_dir="$repo_root/web/src/wasm/ocr-core"
wasm_bindgen="${ITTM_WASM_BINDGEN:-wasm-bindgen}"
sysroot="${ITTM_RUST_WASM_SYSROOT:-$(rustc --print sysroot)}"

RUSTFLAGS="${RUSTFLAGS:-} --sysroot=$sysroot" \
  CARGO_TARGET_DIR="$target_dir" \
  cargo build \
    --manifest-path "$manifest" \
    --release \
    --target wasm32-unknown-unknown \
    --features wasm

mkdir -p "$out_dir"
"$wasm_bindgen" \
  "$target_dir/wasm32-unknown-unknown/release/ittm_ocr_core.wasm" \
  --target web \
  --out-dir "$out_dir" \
  --out-name ittm_ocr_core
