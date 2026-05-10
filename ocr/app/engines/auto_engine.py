from app.engines.base import OcrEngine


class AutoEngine(OcrEngine):
    """Auto-fallback engine that tries Tesseract first (core), then Easy OCR (optional high-quality)."""

    def __init__(self, prefer_tesseract: bool = True):
        from app.engines.stub_engine import StubEngine
        from app.engines.easyocr_engine import EasyOcrEngine
        from app.engines.tesseract_engine import TesseractEngine

        self.tesseract = TesseractEngine()
        self.easy = EasyOcrEngine()
        self.stub = StubEngine()

        # Build priority queue
        engines = []
        if prefer_tesseract:
            engines = [self.tesseract, self.easy, self.stub]
        else:
            engines = [self.easy, self.tesseract, self.stub]

        self.active_engine = next((e for e in engines if e.available()), self.stub)

    def recognize(self, image, mode: str = "text_mode", psm: int = 6) -> str:
        return self.active_engine.recognize(image, mode=mode, psm=psm)

    def available(self) -> bool:
        return self.tesseract.available() or self.easy.available()

    def info(self) -> dict:
        info = self.active_engine.info()
        info["strategy"] = "auto_fallback"
        info["tesseract_available"] = self.tesseract.available()
        info["easyocr_available"] = self.easy.available()
        return info
