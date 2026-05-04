from app.engines.base import OcrEngine

class AutoEngine(OcrEngine):
    def __init__(self):
        from app.engines.stub_engine import StubEngine
        from app.engines.tesseract_engine import TesseractEngine
        
        tes = TesseractEngine()
        if tes.available():
            self.active_engine = tes
        else:
            self.active_engine = StubEngine()

    def recognize(self, image, mode: str = "text_mode", psm: int = 6) -> str:
        return self.active_engine.recognize(image, mode=mode, psm=psm)

    def available(self) -> bool:
        return True

    def info(self) -> dict:
        info = self.active_engine.info()
        info["strategy"] = "auto_fallback"
        return info
