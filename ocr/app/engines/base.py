class OcrEngine:
    def recognize(self, image, mode: str = "text_mode", psm: int = 6) -> str:
        raise NotImplementedError

    def recognize_words(self, image, psm: int = 6, min_conf: int = 20) -> list[dict]:
        return []

    def available(self) -> bool:
        return False

    def info(self) -> dict:
        return {"engine": "base"}
