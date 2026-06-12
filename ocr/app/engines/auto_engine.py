from app.engines.base import OcrEngine


class AutoEngine(OcrEngine):
    """Auto-fallback engine that tries Tesseract first (core), then Easy OCR (optional high-quality)."""

    def __init__(self, prefer_tesseract: bool = True):
        from app.engines.easyocr_engine import EasyOcrEngine
        from app.engines.tesseract_engine import TesseractEngine

        self.tesseract = TesseractEngine()
        self.easy = EasyOcrEngine()

        if prefer_tesseract:
            self.engines = [self.tesseract, self.easy]
        else:
            self.engines = [self.easy, self.tesseract]

        self.active_engine = next((engine for engine in self.engines if engine.available()), None)

    def recognize(self, image, mode: str = "text_mode", psm: int = 6) -> str:
        for engine in self.engines:
            if not engine.available():
                continue

            text = engine.recognize(image, mode=mode, psm=psm)
            if text.strip():
                self.active_engine = engine
                return text

        return ""

    def recognize_words(self, image, psm: int = 6, min_conf: int = 20) -> list[dict]:
        for engine in self.engines:
            if not engine.available():
                continue

            words = engine.recognize_words(image, psm=psm, min_conf=min_conf)
            if words:
                self.active_engine = engine
                return words

        return []

    def available(self) -> bool:
        return self.tesseract.available() or self.easy.available()

    def info(self) -> dict:
        info = self.active_engine.info() if self.active_engine is not None else {"engine": "none", "available": False}
        info["strategy"] = "auto_fallback"
        info["tesseract_available"] = self.tesseract.available()
        info["easyocr_available"] = self.easy.available()
        return info
