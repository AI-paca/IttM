#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
cd "$repo_root"

native_pid=""
gateway_pid=""
port_file="$(mktemp)"
cleanup() {
    if [[ -n "$gateway_pid" ]]; then kill "$gateway_pid" 2>/dev/null || true; fi
    if [[ -n "$native_pid" ]]; then kill "$native_pid" 2>/dev/null || true; fi
    rm -f "$port_file"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ ! -d node_modules ]]; then npm ci; fi
cargo build --locked --release --manifest-path ocr-runtime/Cargo.toml
bash "$script_dir/build-pipeline-core.sh"

local_tessdata="${ITTM_TESSDATA_DIR:-$repo_root/.cache/tessdata}"
if [[ -z "${TESSDATA_PREFIX:-}" && -f "$local_tessdata/eng.traineddata" && -f "$local_tessdata/rus.traineddata" ]]; then
    export TESSDATA_PREFIX="$local_tessdata"
fi
ocr-runtime/target/release/ittm-ocr check
ocr-runtime/target/release/ittm-ocr serve --host 127.0.0.1 --port "${PY_PORT:-0}" --port-file "$port_file" &
native_pid=$!
for _ in {1..100}; do
    [[ -s "$port_file" ]] && break
    kill -0 "$native_pid" 2>/dev/null || { echo "Rust OCR stopped during startup" >&2; exit 1; }
    sleep 0.1
done
[[ -s "$port_file" ]] || { echo "Rust OCR startup timed out" >&2; exit 1; }
ocr_port="$(cat "$port_file")"
export OCR_URL="http://127.0.0.1:$ocr_port"
if [[ -z "${PORT:-}" ]]; then
    PORT="$(node --input-type=module -e 'import net from "node:net"; const s=net.createServer(); s.listen(0,"127.0.0.1",()=>{console.log(s.address().port);s.close();});')"
    export PORT
fi
node --import tsx server.ts &
gateway_pid=$!
echo "Gateway: http://127.0.0.1:$PORT"
echo "Rust OCR: $OCR_URL"
wait -n "$native_pid" "$gateway_pid"
