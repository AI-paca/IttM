class OcrEngine:
    def recognize(self, image, mode: str = "text_mode", psm: int = 6) -> str:
        raise NotImplementedError

    def available(self) -> bool:
        return False

    def info(self) -> dict:
        return {"engine": "base"}
