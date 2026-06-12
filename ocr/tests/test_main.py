import asyncio
import io
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import upload_limits
from app.main import app
from app.routers import install
from app.services import convert_service

client = TestClient(app)


def test_probe():
    response = client.post("/probe", json={"modes": ["all"], "engines": ["auto"]})
    assert response.status_code == 200
    json_data = response.json()
    assert json_data["ok"] is True
    assert "cases" in json_data


def test_probe_v1_alias():
    response = client.post("/v1/probe", json={"modes": ["all"], "engines": ["auto"]})
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    json_data = response.json()
    assert json_data["ok"] is True
    assert json_data["service"] == "Python OCR Service"


def test_health_v1_alias():
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_readiness():
    response = client.get("/readiness")
    assert response.status_code == 200
    json_data = response.json()
    assert isinstance(json_data["ready"], bool)
    assert set(json_data["checks"]) >= {
        "tesseract",
        "pdftoppm",
        "pytesseract",
        "pdf2image",
        "opencv",
    }


def test_diagnostics_v1_alias():
    response = client.get("/v1/diagnostics")
    assert response.status_code == 200
    json_data = response.json()
    assert "python_version" in json_data
    assert "cpu_cores" in json_data
    assert "torch_available" in json_data
    assert "easyocr_available" in json_data
    assert "torch_error" in json_data


def test_easyocr_install_uses_supported_pip_progress_mode():
    command = install._pip_install_command()

    assert command[command.index("--progress-bar") + 1] == "on"
    assert "easyocr" in command
    assert "torch" in command


def _reset_install_job():
    install._worker = None
    with install._job_lock:
        install._job.status = "idle"
        install._job.phase = "idle"
        install._job.message = "EasyOCR is not being installed."
        install._job.progress = 0
        install._job.logs.clear()


def test_install_easyocr_disabled_by_runtime_flag(monkeypatch):
    _reset_install_job()
    monkeypatch.setenv("DISABLE_RUNTIME_EASYOCR_INSTALL", "1")

    response = client.post("/v1/install-easyocr")

    assert response.status_code == 409
    assert response.json()["status"] == "disabled"


def test_install_easyocr_starts_background_worker(monkeypatch):
    _reset_install_job()
    monkeypatch.delenv("DISABLE_RUNTIME_EASYOCR_INSTALL", raising=False)
    started = []

    class FakeThread:
        def __init__(self, target, daemon=False):
            self.target = target
            self.daemon = daemon

        def start(self):
            started.append((self.target, self.daemon))

    monkeypatch.setattr(install.threading, "Thread", FakeThread)

    try:
        response = client.post("/v1/install-easyocr")
        payload = response.json()

        assert response.status_code == 200
        assert payload["status"] == "running"
        assert payload["phase"] == "starting"
        assert started == [(install._run_install_job, True)]
    finally:
        _reset_install_job()


def test_convert_invalid_pdf():
    # Sending a broken pdf
    response = client.post(
        "/convert?engine_type=auto", files={"file": ("test.pdf", b"%PDF-1.4...invalid", "application/pdf")}
    )
    # Our new exception handler should return 400 Bad Request
    assert response.status_code == 400
    assert "Failed to process PDF" in response.json()["detail"]


def test_convert_invalid_image():
    # Sending a totally invalid text file as an image
    response = client.post("/convert?engine_type=auto", files={"file": ("test.png", b"not an image", "image/png")})
    # Our new exception handler should return 400 Bad Request
    assert response.status_code == 400
    assert "Could not load image" in response.json()["detail"]


def test_convert_uses_service_contract(monkeypatch):
    def fake_convert(content, filename, engine_type="auto", pipeline_profile=None):
        assert content.startswith(b"\x89PNG")
        assert filename == "test.png"
        assert engine_type == "auto"
        assert pipeline_profile is not None
        return "FAKE OCR MARKDOWN", {
            "engine": "fake",
            "chunks": 1,
            "cards_found": 0,
            "pages": 1,
            "elapsed_ms": 0,
        }

    monkeypatch.setattr(convert_service, "convert_bytes", fake_convert)

    img = Image.new("RGB", (100, 30), color=(255, 255, 255))
    img_bytes = io.BytesIO()
    img.save(img_bytes, format="PNG")
    img_bytes.seek(0)

    response = client.post("/convert?engine_type=auto", files={"file": ("test.png", img_bytes, "image/png")})
    assert response.status_code == 200
    json_data = response.json()
    assert json_data["markdown"] == "FAKE OCR MARKDOWN"
    assert json_data["meta"]["engine"] == "fake"
    assert json_data["meta"]["chunks"] == 1


def test_convert_does_not_block_health_endpoint(monkeypatch):
    started = threading.Event()

    def slow_convert(content, filename, engine_type="auto", pipeline_profile=None):
        started.set()
        time.sleep(1)
        return "done", {
            "engine": "fake",
            "chunks": 1,
            "cards_found": 0,
            "pages": 1,
            "elapsed_ms": 0,
        }

    monkeypatch.setattr(convert_service, "convert_bytes", slow_convert)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
            convert_task = asyncio.create_task(
                async_client.post(
                    "/convert?engine_type=auto",
                    files={"file": ("test.png", b"image bytes", "image/png")},
                )
            )
            assert await asyncio.to_thread(started.wait, 0.5)

            health_start = time.monotonic()
            health_response = await async_client.get("/health")
            health_elapsed = time.monotonic() - health_start
            convert_response = await convert_task

        assert health_response.status_code == 200
        assert health_elapsed < 0.5
        assert convert_response.status_code == 200

    asyncio.run(scenario())


def test_convert_rejects_upload_over_configured_limit(monkeypatch):
    monkeypatch.setattr(upload_limits, "max_upload_bytes", lambda: 8)

    response = client.post(
        "/convert?engine_type=auto",
        files={"file": ("test.png", b"123456789", "image/png")},
    )

    assert response.status_code == 413
    assert "8 byte upload limit" in response.json()["detail"]


def test_convert_rejects_empty_upload():
    response = client.post(
        "/convert?engine_type=auto",
        files={"file": ("test.png", b"", "image/png")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Uploaded file is empty"


def test_image_bytes_do_not_use_pdf_temp_directory(monkeypatch):
    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            return "image text"

        def info(self):
            return {"engine": "fake"}

    def fail_temp_directory(*args, **kwargs):
        pytest.fail("image conversion must not create a PDF temp directory")

    monkeypatch.setattr(convert_service, "AutoEngine", lambda prefer_tesseract=True: FakeEngine())
    monkeypatch.setattr(convert_service.tempfile, "TemporaryDirectory", fail_temp_directory)

    image = Image.new("RGB", (100, 30), color="white")
    content = io.BytesIO()
    image.save(content, format="PNG")

    markdown, meta = convert_service.convert_bytes(content.getvalue(), filename="test.png", engine_type="auto")

    assert markdown == "image text"
    assert meta["pages"] == 1
