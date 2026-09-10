# Runtime scripts

These scripts are implementation entry points for local runtime and build
tasks.

| Script                                          | Purpose                                    |
| ----------------------------------------------- | ------------------------------------------ |
| `scripts/runtime/run-local.sh`                  | Start the web/gateway and local Python OCR |
| `scripts/runtime/install-local-python.sh`       | Create/update the local Python environment |
| `scripts/runtime/build-lite.sh`                 | Build the browser-only distribution        |
| `scripts/runtime/build-pipeline-core.sh`        | Build the WASM shared core                 |
| `scripts/runtime/build-pipeline-core-native.sh` | Build the native shared core               |
| `scripts/runtime/notify-docker-restart.sh`      | Optional local restart notification helper |

Run from the repository root:

```bash
bash scripts/runtime/run-local.sh
bash scripts/runtime/build-lite.sh
```

EasyOCR is optional:

```bash
INSTALL_EASYOCR=1 bash scripts/runtime/install-local-python.sh
```

The script source and its error output are authoritative for prerequisites and
environment variables.
