"""Optional persistent EasyOCR worker for the Rust host; stdout is the protocol."""

import base64
import contextlib
import json
import sys
from pathlib import Path

from PIL import Image


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.engines.easyocr_engine import EasyOcrEngine

    output = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        engine = EasyOcrEngine(download_enabled=False)
        if not engine.available():
            raise RuntimeError("EasyOCR and its model files must be installed before starting the worker")
    for line in sys.stdin:
        try:
            request = json.loads(line)
            width, height = int(request["width"]), int(request["height"])
            if width <= 0 or height <= 0 or width * height > 80_000_000:
                raise ValueError("Invalid OCR image dimensions")
            pixels = base64.b64decode(request["rgb"], validate=True)
            if len(pixels) != width * height * 3:
                raise ValueError("Invalid RGB buffer length")
            with Image.frombytes("RGB", (width, height), pixels) as image:
                with contextlib.redirect_stdout(sys.stderr):
                    raw = engine.recognize_words(image, min_conf=0)
            words = [
                {
                    "text": word["text"],
                    "bbox": list(word["bbox"]),
                    "confidence_ppm": round(max(0, min(100, word["conf"])) * 10000),
                }
                for word in raw
            ]
            response = {"text": " ".join(word["text"] for word in words), "words": words}
        except Exception as exc:
            response = {"error": str(exc)}
        output.write(json.dumps(response, ensure_ascii=False) + "\n")
        output.flush()


if __name__ == "__main__":
    main()
