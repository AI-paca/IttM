import io

from fastapi.testclient import TestClient
from PIL import Image

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
    async def fake_convert(path, engine_type="auto", pipeline_profile=None):
        assert path.exists()
        assert engine_type == "auto"
        assert pipeline_profile is not None
        return "FAKE OCR MARKDOWN", {
            "engine": "fake",
            "chunks": 1,
            "cards_found": 0,
            "pages": 1,
            "elapsed_ms": 0,
        }

    monkeypatch.setattr(convert_service, "convert", fake_convert)

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
