from app.engines.base import OcrEngine

class AutoEngine(OcrEngine):
    """Auto-fallback engine that tries Tesseract first (core), then EasyOCR (optional high-quality)."""
    
    def __init__(self, prefer_tesseract: bool = True):
        """
        Initialize auto engine.
        
        Args:
            prefer_tesseract: If True, use Tesseract as primary (core), EasyOCR as fallback.
                            If False, use EasyOCR as primary (high-quality mode).
        """
        from app.engines.stub_engine import StubEngine
        from app.engines.tesseract_engine import TesseractEngine
        from app.engines.easyocr_engine import EasyOcrEngine
        
        self.tesseract = TesseractEngine()
        self.easyocr = EasyOcrEngine()
        self.stub = StubEngine()
        
        if prefer_tesseract:
            self.active_engine = self.tesseract if self.tesseract.available() else (self.easyocr if self.easyocr.available() else self.stub)
        else:
            self.active_engine = self.easyocr if self.easyocr.available() else (self.tesseract if self.tesseract.available() else self.stub)

    def recognize(self, image, mode: str = "text_mode", psm: int = 6) -> str:
        return self.active_engine.recognize(image, mode=mode, psm=psm)

    def available(self) -> bool:
        return self.tesseract.available() or self.easyocr.available()

    def info(self) -> dict:
        info = self.active_engine.info()
        info["strategy"] = "auto_fallback"
        info["tesseract_available"] = self.tesseract.available()
        info["easyocr_available"] = self.easyocr.available()
        return info
