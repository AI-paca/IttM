import io

from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
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


def test_readiness():
    response = client.get("/readiness")
    assert response.status_code == 200
    assert response.json()["ready"] is True


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
    async def fake_convert(path, engine_type="auto"):
        assert path.exists()
        assert engine_type == "auto"
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
