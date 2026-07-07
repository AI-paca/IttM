from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from PIL import Image


WordBox = tuple[int, int, int, int]


@dataclass(frozen=True)
class RecognizedWord:
    text: str
    bbox: WordBox
    conf: float


@dataclass(frozen=True)
class OcrTextCandidate:
    language: str
    text: str
    source: str = "ocr"


@dataclass(frozen=True)
class OcrWordCandidate:
    language: str
    words: list[dict]
    source: str = "ocr"


@dataclass(frozen=True)
class SegmentOcrRequest:
    image: Image.Image
    psm: int
    mode: str = "text_mode"
    min_conf: int = 20


@dataclass(frozen=True)
class SegmentOcrResult:
    text: str
    chunks: int
    runtime_flags: tuple[str, ...] = ()


class TextOcrEngine(Protocol):
    def recognize(
        self,
        image: Image.Image,
        mode: str = "text_mode",
        psm: int = 6,
    ) -> str:
        ...


class WordOcrEngine(Protocol):
    def recognize_words(
        self,
        image: Image.Image,
        psm: int = 6,
        min_conf: int = 20,
    ) -> list[dict]:
        ...
