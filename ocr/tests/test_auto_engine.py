from PIL import Image

from app.engines.auto_engine import AutoEngine


class EmptyEngine:
    def __init__(self, name: str):
        self.name = name

    def available(self) -> bool:
        return True

    def recognize(self, image, mode: str = "text_mode", psm: int = 6) -> str:
        return ""

    def recognize_words(self, image, psm: int = 6, min_conf: int = 20) -> list[dict]:
        return []

    def info(self) -> dict:
        return {"engine": self.name, "available": True}


def test_auto_engine_does_not_fall_back_to_probe_stub(monkeypatch):
    tesseract = EmptyEngine("tesseract")
    easyocr = EmptyEngine("easyocr")

    monkeypatch.setattr(
        "app.engines.tesseract_engine.TesseractEngine",
        lambda: tesseract,
    )
    monkeypatch.setattr(
        "app.engines.easyocr_engine.EasyOcrEngine",
        lambda: easyocr,
    )

    engine = AutoEngine()
    image = Image.new("RGB", (20, 20), "white")

    assert engine.recognize(image) == ""
    assert engine.recognize_words(image) == []
    assert [candidate.info()["engine"] for candidate in engine.engines] == [
        "tesseract",
        "easyocr",
    ]
