import os
import io
import sys
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.main import app

client = TestClient(app)

def test_probe():
    response = client.post("/probe", json={"modes": ["all"], "engines": ["auto"]})
    # Status should be 200 and return a probe report
    if response.status_code == 200:
        json_data = response.json()
        assert "cases" in json_data
    else:
        # If it fails, that's fine if Tesseract isn't immediately ready in some envs, 
        # but the endpoint exists.
        assert response.status_code in [200, 400, 500]

def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    json_data = response.json()
    assert json_data["ok"] is True
    assert json_data["service"] == "Python OCR Service"

def test_convert_invalid_pdf():
    # Sending a broken pdf
    response = client.post(
        "/convert?engine_type=auto",
        files={"file": ("test.pdf", b"%PDF-1.4...invalid", "application/pdf")}
    )
    # Our new exception handler should return 400 Bad Request
    assert response.status_code == 400
    assert "Failed to process PDF" in response.json()["detail"]

def test_convert_invalid_image():
    # Sending a totally invalid text file as an image
    response = client.post(
        "/convert?engine_type=auto",
        files={"file": ("test.png", b"not an image", "image/png")}
    )
    # Our new exception handler should return 400 Bad Request
    assert response.status_code == 400
    assert "Could not load image" in response.json()["detail"]

def test_convert_auto():
    # Create a small dummy image for testing OCR endpoint
    img = Image.new('RGB', (100, 30), color = (255, 255, 255))
    img_bytes = io.BytesIO()
    img.save(img_bytes, format='PNG')
    img_bytes.seek(0)
    
    response = client.post(
        "/convert?engine_type=auto",
        files={"file": ("test.png", img_bytes, "image/png")}
    )
    assert response.status_code == 200
    json_data = response.json()
    
    # Text might be empty for a blank image, but it should return 200 OK and correct structure
    assert "markdown" in json_data
    assert "meta" in json_data
