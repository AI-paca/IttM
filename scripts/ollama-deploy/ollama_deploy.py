#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LOCAL_API_CONTAINER_PATH = Path("local_ocr_api.py")
CATALOG_PATH = ROOT / "models.json"
ENV_PATHS = (ROOT / ".env", ROOT / ".evn")


def load_catalog() -> dict[str, Any]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def load_env_files() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in ENV_PATHS:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = os.path.expandvars(value.strip().strip("'\""))
    return values


def merged_env() -> dict[str, str]:
    env = os.environ.copy()
    for key, value in load_env_files().items():
        env.setdefault(key, value)
    env.setdefault("HF_HOME", str(Path.home() / ".cache" / "huggingface"))
    env.setdefault(
        "NEMOTRON_OCR_REPO_DIR",
        str(Path.home() / ".cache" / "huggingface" / "nemotron-ocr-v2-src"),
    )
    env.setdefault("OLLAMA_HOST", "http://127.0.0.1:11434")
    env.setdefault("OLLAMA_DEPLOY_API_HOST", "127.0.0.1")
    env.setdefault("OLLAMA_DEPLOY_API_PORT", "18080")
    return env


def write_env_value(key: str, value: str) -> None:
    path = ROOT / ".env"
    lines: list[str] = []
    found = False
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    elif (ROOT / ".env.example").exists():
        lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[index] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def select_model(catalog: dict[str, Any], requested: str | None) -> dict[str, Any]:
    models = catalog["models"]
    if requested:
        for model in models:
            if model["id"] == requested:
                return model
        known = ", ".join(model["id"] for model in models)
        raise SystemExit(f"Unknown model '{requested}'. Known models: {known}")
    if not sys.stdin.isatty():
        raise SystemExit("Use --model in non-interactive mode.")
    print("Choose model:")
    for index, model in enumerate(models, 1):
        print(f"  {index}. {model['id']} - {model['label']}")
    choice = input("Model number: ").strip()
    try:
        return models[int(choice) - 1]
    except (ValueError, IndexError) as exc:
        raise SystemExit("Invalid model selection.") from exc


def select_backend(
    model: dict[str, Any], requested: str | None
) -> tuple[str, dict[str, Any]]:
    backends = model["backends"]
    name = requested or model["default_backend"]
    if name not in backends:
        known = ", ".join(sorted(backends))
        raise SystemExit(
            f"Unknown backend '{name}' for {model['id']}. Known backends: {known}"
        )
    return name, backends[name]


def ensure_hf_token(env: dict[str, str], dry_run: bool) -> None:
    if env.get("HF_TOKEN") or env.get("HUGGING_FACE_HUB_TOKEN") or dry_run:
        return
    if not sys.stdin.isatty():
        print("HF_TOKEN is not set; downloads will use anonymous Hugging Face access.")
        return
    token = getpass.getpass(
        "HF token is not set. Paste token for gated/rate-limited downloads, "
        "or press Enter for anonymous access: "
    ).strip()
    if token:
        write_env_value("HF_TOKEN", token)
        env["HF_TOKEN"] = token
        print(f"Saved HF_TOKEN to {ROOT / '.env'}")
    else:
        print("Continuing with anonymous Hugging Face access.")


def print_model_list(catalog: dict[str, Any]) -> None:
    for model in catalog["models"]:
        backends = ", ".join(sorted(model["backends"]))
        print(f"{model['id']}\t{model['label']}\tbackends={backends}")


def print_flag_contract(model: dict[str, Any]) -> None:
    print(f"Flag contract for {model['id']}:")
    for flag in model["flag_contract"]:
        print(
            f"  [{flag['requirement']}] {flag['group']}:{flag['key']} - "
            f"{flag['reason']}"
        )


def maybe_run(command: list[str], env: dict[str, str], dry_run: bool) -> int:
    print("$ " + shlex.join(command))
    if dry_run:
        return 0
    return subprocess.run(command, env=env, check=False).returncode


def hf_download_command(
    model: dict[str, Any], backend: dict[str, Any], env: dict[str, str]
) -> list[str] | None:
    if shutil.which("huggingface-cli") is None:
        return None
    command = [
        "huggingface-cli",
        "download",
        model["hf_repo"],
        "--cache-dir",
        env["HF_HOME"],
    ]
    include = backend.get("download_include")
    if include:
        command.extend(["--include", include])
    return command


def build_download_command(
    model: dict[str, Any], backend: dict[str, Any], env: dict[str, str]
) -> list[str] | None:
    if backend["kind"] == "ollama":
        return ["ollama", "pull", backend["ollama_model"]]
    if backend.get("server_backend") == "nemotron":
        source_dir = Path(env["NEMOTRON_OCR_REPO_DIR"]).expanduser()
        if source_dir.exists():
            print(f"Using existing Nemotron source cache: {source_dir}")
            return None
        source_dir.parent.mkdir(parents=True, exist_ok=True)
        return [
            "git",
            "clone",
            "https://huggingface.co/nvidia/nemotron-ocr-v2",
            str(source_dir),
        ]
    if model.get("hf_repo"):
        return hf_download_command(model, backend, env)
    return None


def ollama_is_running(env: dict[str, str]) -> bool:
    host = env.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=1.5) as response:
            return 200 <= response.status < 500
    except (OSError, urllib.error.URLError):
        return False


def vllm_command(
    model: dict[str, Any],
    backend: dict[str, Any],
    env: dict[str, str],
    host: str,
    port: int,
    use_docker: bool,
) -> list[str]:
    extra_args = list(backend.get("extra_args", []))
    if use_docker:
        command = [
            "docker",
            "run",
            "--rm",
            "--gpus",
            "all",
            "--ipc=host",
            "--shm-size",
            "32g",
            "-p",
            f"{port}:8000",
            "-v",
            f"{env['HF_HOME']}:/root/.cache/huggingface",
            "-e",
            "HF_TOKEN",
            backend.get("docker_image", "vllm/vllm-openai:nightly"),
            "--model",
            model["hf_repo"],
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
        ]
        command.extend(extra_args)
        return command
    return [
        "vllm",
        "serve",
        model["hf_repo"],
        "--host",
        host,
        "--port",
        str(port),
        *extra_args,
    ]


def local_fastapi_command(
    backend: dict[str, Any],
    env: dict[str, str],
    host: str,
    port: int,
    use_docker: bool,
) -> list[str]:
    server_backend = backend["server_backend"]
    if use_docker:
        if server_backend == "paddle":
            install = (
                "python -m pip install --upgrade pip && "
                "python -m pip install fastapi uvicorn python-multipart pillow "
                "paddleocr transformers torch "
            )
            image = "python:3.12"
            volumes = []
            prefix = ""
        else:
            install = (
                "cd /nemotron-ocr-src && "
                "python -m pip install --no-build-isolation -v . && "
                "cd /workspace && "
                "python -m pip install fastapi uvicorn python-multipart pillow "
            )
            image = "nvcr.io/nvidia/pytorch:25.09-py3"
            source_dir = str(Path(env["NEMOTRON_OCR_REPO_DIR"]).expanduser())
            volumes = ["-v", f"{source_dir}:/nemotron-ocr-src"]
            prefix = ""
        run = (
            f"{prefix}{install}&& python3 {shlex.quote(str(LOCAL_API_CONTAINER_PATH))} "
            f"--backend {shlex.quote(server_backend)} --host 0.0.0.0 --port {port}"
        )
        return [
            "docker",
            "run",
            "--rm",
            "--gpus",
            "all",
            "-p",
            f"{port}:{port}",
            "-v",
            f"{ROOT}:/workspace",
            "-v",
            f"{env['HF_HOME']}:/root/.cache/huggingface",
            *volumes,
            "-e",
            "HF_TOKEN",
            "-w",
            "/workspace",
            image,
            "bash",
            "-lc",
            run,
        ]
    return [
        "python3",
        str(ROOT / "local_ocr_api.py"),
        "--backend",
        server_backend,
        "--host",
        host,
        "--port",
        str(port),
    ]


def build_server_command(
    model: dict[str, Any],
    backend: dict[str, Any],
    env: dict[str, str],
    host: str,
    port: int,
    use_docker: bool,
) -> list[str] | None:
    kind = backend["kind"]
    if kind == "ollama":
        return ["ollama", "serve"]
    if kind == "vllm":
        return vllm_command(model, backend, env, host, port, use_docker)
    if kind == "local-fastapi":
        return local_fastapi_command(backend, env, host, port, use_docker)
    raise SystemExit(f"Unsupported backend kind: {kind}")


def api_url(
    model: dict[str, Any], backend: dict[str, Any], host: str, port: int
) -> str:
    if backend["kind"] == "ollama":
        return backend["api_url"]
    if backend["kind"] == "vllm":
        return f"http://{host}:{port}/v1/chat/completions"
    return f"http://{host}:{port}/v1/ocr"


def main() -> int:
    parser = argparse.ArgumentParser(description="Select and run a local OCR/VLM API.")
    parser.add_argument("--list", action="store_true", help="List known models.")
    parser.add_argument(
        "--model",
        help="Model id from scripts/ollama-deploy/models.json.",
    )
    parser.add_argument("--backend", help="Backend name for the selected model.")
    parser.add_argument(
        "--docker",
        action="store_true",
        help="Run through a disposable Docker container when supported.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without running downloads or servers.",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Let the backend download lazily or use existing cache.",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Download/cache the model and exit.",
    )
    parser.add_argument(
        "--show-flags",
        action="store_true",
        help="Show functional flag contract for the selected model.",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="API host. Defaults to OLLAMA_DEPLOY_API_HOST.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="API port. Defaults to backend/model or OLLAMA_DEPLOY_API_PORT.",
    )
    args = parser.parse_args()

    catalog = load_catalog()
    if args.list:
        print_model_list(catalog)
        return 0

    env = merged_env()
    model = select_model(catalog, args.model)
    backend_name, backend = select_backend(model, args.backend)
    host = args.host or env["OLLAMA_DEPLOY_API_HOST"]
    port = args.port or int(backend.get("port") or env["OLLAMA_DEPLOY_API_PORT"])

    print(f"Selected model: {model['id']} ({model['label']})")
    print(f"Selected backend: {backend_name} ({backend['kind']})")
    if args.show_flags:
        print_flag_contract(model)

    ensure_hf_token(env, dry_run=args.dry_run or backend["kind"] == "ollama")
    Path(env["HF_HOME"]).expanduser().mkdir(parents=True, exist_ok=True)
    Path(env["NEMOTRON_OCR_REPO_DIR"]).expanduser().parent.mkdir(
        parents=True, exist_ok=True
    )

    if not args.skip_download:
        download_command = build_download_command(model, backend, env)
        if download_command:
            code = maybe_run(download_command, env, args.dry_run)
            if code != 0:
                return code
        else:
            print(
                "No explicit downloader found; backend will use its own cache/download path."
            )

    print(f"Local API: {api_url(model, backend, host, port)}")
    if args.download_only:
        return 0

    if backend["kind"] == "ollama" and not args.dry_run and ollama_is_running(env):
        print("Ollama is already running; API is ready.")
        return 0

    command = build_server_command(model, backend, env, host, port, args.docker)
    if command is None:
        return 0
    return maybe_run(command, env, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
