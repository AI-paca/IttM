#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
output_dir="$repo_root/web/public/wasm"
build_tmp="$(mktemp -d)"
cache_dir="${XDG_CACHE_HOME:-$HOME/.cache}/ittm-pipeline-core"
rust_image="rust@sha256:1f0dbad1df66647807e6952d1db85d0b2bda7606cb2139d82517e4f009967376"
arch_rust_wasm_url="https://fastly.mirror.pkgbuild.com/extra/os/x86_64/rust-wasm-1%3A1.96.1-1-x86_64.pkg.tar.zst"
arch_rust_wasm_sha256="d7115ea2798605be64cdbbb9030b560b26aecabc59bc5dda2c6da9623a434d9b"
arch_rust_host_package="${ARCH_RUST_HOST_PACKAGE:-}"
trap 'rm -rf "$build_tmp"' EXIT

mkdir -p "$output_dir"
mkdir -p "$cache_dir"

if [[ -z "$arch_rust_host_package" && -d /var/cache/pacman/pkg ]]; then
  arch_rust_host_package="$(
    find /var/cache/pacman/pkg -maxdepth 1 -type f \
      -name 'rust-1:1.96.1-*.pkg.tar.zst' -print -quit 2>/dev/null || true
  )"
fi

host_sysroot="$(rustc --print sysroot)"
if [[ -d "$host_sysroot/lib/rustlib/wasm32-unknown-unknown" ]]; then
  cargo build \
    --locked \
    --release \
    --target wasm32-unknown-unknown \
    --manifest-path "$repo_root/pipeline-core/Cargo.toml"
  wasm_source="$repo_root/pipeline-core/target/wasm32-unknown-unknown/release/ittm_pipeline_core.wasm"
elif command -v rustup >/dev/null 2>&1; then
  rustup target add wasm32-unknown-unknown
  cargo build \
    --locked \
    --release \
    --target wasm32-unknown-unknown \
    --manifest-path "$repo_root/pipeline-core/Cargo.toml"
  wasm_source="$repo_root/pipeline-core/target/wasm32-unknown-unknown/release/ittm_pipeline_core.wasm"
elif [[ "$(rustc --version)" == rustc\ 1.96.1* ]]; then
  package="$cache_dir/rust-wasm-1.96.1-1.pkg.tar.zst"
  sysroot="$build_tmp/sysroot"
  mkdir -p "$sysroot"
  if [[ ! -f "$package" ]]; then
    curl -fsSL --retry 3 -o "$package" "$arch_rust_wasm_url"
  fi
  printf '%s  %s\n' "$arch_rust_wasm_sha256" "$package" | sha256sum --check --status
  bsdtar -xf "$package" -C "$sysroot"
  RUSTFLAGS="--sysroot=$sysroot/usr" cargo build \
    --locked \
    --release \
    --target wasm32-unknown-unknown \
    --manifest-path "$repo_root/pipeline-core/Cargo.toml"
  wasm_source="$repo_root/pipeline-core/target/wasm32-unknown-unknown/release/ittm_pipeline_core.wasm"
elif [[ -s "$cache_dir/rust-wasm-1.96.1-1.pkg.tar.zst" && \
        -n "$arch_rust_host_package" && -s "$arch_rust_host_package" ]] && \
     command -v bsdtar >/dev/null 2>&1 && command -v wasm-ld >/dev/null 2>&1; then
  package="$cache_dir/rust-wasm-1.96.1-1.pkg.tar.zst"
  sysroot="$build_tmp/arch-rust"
  target="$build_tmp/arch-target"
  mkdir -p "$sysroot" "$target"
  printf '%s  %s\n' "$arch_rust_wasm_sha256" "$package" | sha256sum --check --status
  bsdtar -xf "$arch_rust_host_package" -C "$sysroot"
  bsdtar -xf "$package" -C "$sysroot"
  env \
    PATH="$sysroot/usr/bin:$PATH" \
    LD_LIBRARY_PATH="$sysroot/usr/lib" \
    RUSTC="$sysroot/usr/bin/rustc" \
    CARGO_TARGET_DIR="$target" \
    CARGO_TARGET_WASM32_UNKNOWN_UNKNOWN_LINKER="$(command -v wasm-ld)" \
    cargo build \
      --locked \
      --release \
      --target wasm32-unknown-unknown \
      --manifest-path "$repo_root/pipeline-core/Cargo.toml"
  wasm_source="$target/wasm32-unknown-unknown/release/ittm_pipeline_core.wasm"
else
  container_output="$build_tmp/container"
  mkdir -p "$container_output"
  docker run --rm \
    -v "$repo_root:/work:ro" \
    -v "$container_output:/out" \
    -w /work/pipeline-core \
    -e CARGO_TARGET_DIR=/out/target \
    "$rust_image" \
    bash -c 'rustup target add wasm32-unknown-unknown && cargo build --locked --release --target wasm32-unknown-unknown && chmod -R a+rwX /out'
  wasm_source="$container_output/target/wasm32-unknown-unknown/release/ittm_pipeline_core.wasm"
fi

cp \
  "$wasm_source" \
  "$output_dir/ittm_pipeline_core.wasm"

node \
  "$repo_root/scripts/ci/verify-pipeline-core-wasm.mjs" \
  "$output_dir/ittm_pipeline_core.wasm"

printf '%s\n' "$output_dir/ittm_pipeline_core.wasm"
