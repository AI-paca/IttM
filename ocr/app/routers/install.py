import os
import subprocess
import sys
import threading
from os import environ, makedirs
from collections import deque
from dataclasses import dataclass, field

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

router = APIRouter()


@dataclass
class InstallJob:
    status: str = "idle"
    phase: str = "idle"
    message: str = "EasyOCR is not being installed."
    progress: int = 0
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=80))


_job = InstallJob()
_job_lock = threading.Lock()
_worker: threading.Thread | None = None


def _snapshot() -> dict:
    with _job_lock:
        return {
            "status": _job.status,
            "phase": _job.phase,
            "message": _job.message,
            "progress": _job.progress,
            "logs": list(_job.logs),
        }


def _update(*, status: str | None = None, phase: str | None = None, message: str, progress: int | None = None):
    line = f"[install-easyocr] {message}"
    print(line, flush=True)
    with _job_lock:
        if status is not None:
            _job.status = status
        if phase is not None:
            _job.phase = phase
        _job.message = message
        if progress is not None:
            _job.progress = max(0, min(100, progress))
        _job.logs.append(line)


def _pip_install_command() -> list[str]:
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--progress-bar",
        "on",
    ]
    target = environ.get("EASY_INSTALL_TARGET")
    if target:
        makedirs(target, exist_ok=True)
        if target not in sys.path:
            sys.path.insert(0, target)
        command.extend(["--target", target])

    command.extend(["easyocr", "torch", "torchvision"])
    return command


def _pip_install_easyocr() -> dict | None:
    _update(phase="pip_install", message="Installing EasyOCR Python packages...", progress=10)
    process = subprocess.Popen(
        _pip_install_command(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    assert process.stdout is not None
    last_line = ""
    network_error_seen = False
    for raw_line in process.stdout:
        line = raw_line.strip()
        if not line:
            continue
        last_line = line
        if "Network is unreachable" in line or "Failed to establish a new connection" in line:
            network_error_seen = True
        _update(phase="pip_install", message=line[-300:], progress=25)

    returncode = process.wait(timeout=1800)
    if returncode == 0:
        _update(phase="pip_install", message="Python packages are installed.", progress=60)
        return None

    if network_error_seen:
        last_line = (
            "Docker OCR container cannot reach PyPI. Restart Docker daemon with "
            "`sudo systemctl restart docker`, then run `docker compose up -d` again."
        )

    _update(status="error", phase="pip_install", message="pip install failed.", progress=100)
    return {
        "status": "error",
        "message": (last_line or "pip install failed").strip()[-1000:],
        "phase": "pip_install",
    }


def _download_easyocr_models() -> dict:
    import torch
    import easyocr

    _update(phase="model_download", message="Preparing EasyOCR models for languages: en, ru", progress=70)
    use_gpu = torch.cuda.is_available() or (hasattr(torch.backends, "mps") and torch.backends.mps.is_available())
    easyocr.Reader(["en", "ru"], gpu=use_gpu, download_enabled=True)
    _update(status="installed", phase="done", message="EasyOCR package and models are ready.", progress=100)
    return {"status": "installed", "message": "EasyOCR package and models are ready."}


def _network_download_error(message: str) -> str:
    return (
        "EasyOCR package is installed, but model download failed: "
        f"{message}. Check internet/proxy access from the Python environment, "
        "then retry the install button."
    )


def _run_install_job():
    try:
        try:
            import easyocr
        except ImportError:
            install_error = _pip_install_easyocr()
            if install_error:
                _update(
                    status="error",
                    phase=install_error["phase"],
                    message=install_error["message"],
                    progress=100,
                )
                return
        else:
            easyocr  # noqa: B018
            _update(phase="pip_install", message="EasyOCR package is already installed.", progress=60)

        try:
            _download_easyocr_models()
        except Exception as exc:
            _update(
                status="error",
                phase="model_download",
                message=_network_download_error(str(exc)),
                progress=100,
            )
    except subprocess.TimeoutExpired:
        _update(status="error", phase="pip_install", message="pip install timed out after 30 minutes", progress=100)
    except Exception as exc:
        _update(status="error", phase="unknown", message=str(exc), progress=100)


@router.post("/install-easyocr")
@router.post("/v1/install-easyocr")
async def install_easyocr():
    global _worker
    if os.environ.get("DISABLE_RUNTIME_EASYOCR_INSTALL") == "1":
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "status": "disabled",
                "message": (
                    "Runtime EasyOCR install is disabled for this environment. "
                    "Install full OCR dependencies before starting the service, "
                    "or restart with INSTALL_EASYOCR=1 for local setup."
                ),
            },
        )

    already_running = False
    with _job_lock:
        if _job.status == "running":
            already_running = True
        else:
            _job.status = "running"
            _job.phase = "starting"
            _job.message = "Starting EasyOCR installation..."
            _job.progress = 3
            _job.logs.clear()

    if already_running:
        return _snapshot()

    _update(status="running", phase="starting", message="Starting EasyOCR installation...", progress=3)
    _worker = threading.Thread(target=_run_install_job, daemon=True)
    _worker.start()
    return _snapshot()


@router.get("/install-easyocr/status")
@router.get("/v1/install-easyocr/status")
async def install_easyocr_status():
    return _snapshot()
