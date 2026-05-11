from app.engines.base import OcrEngine


class StubEngine(OcrEngine):
    def recognize(self, image, mode: str = "text_mode", psm: int = 6) -> str:
        # Stub logic. Normally would just return stub text.
        return "OCR PROBE\nMARKDOWN TEST\n12345\nitem one\nitem two"

    def available(self) -> bool:
        return True

    def info(self) -> dict:
        return {"engine": "stub", "version": "1.0", "device": "cpu"}
