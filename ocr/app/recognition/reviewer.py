from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.recognition.quality import (
    should_retry_word_languages,
    text_noise_ratio,
    text_quality_score,
    word_candidate_quality,
    words_to_text,
)


class RecognitionCandidateReviewer(Protocol):
    def text_score(self, text: str) -> float: ...

    def text_noise_ratio(self, text: str) -> float: ...

    def word_quality(self, words: list[dict]) -> float: ...

    def should_retry_word_candidates(self, words: list[dict]) -> bool: ...

    def words_to_text(self, words: list[dict]) -> str: ...


@dataclass(frozen=True)
class HeuristicCandidateReviewer:
    def text_score(self, text: str) -> float:
        return text_quality_score(text)

    def text_noise_ratio(self, text: str) -> float:
        return text_noise_ratio(text)

    def word_quality(self, words: list[dict]) -> float:
        return word_candidate_quality(words)

    def should_retry_word_candidates(self, words: list[dict]) -> bool:
        return should_retry_word_languages(words)

    def words_to_text(self, words: list[dict]) -> str:
        return words_to_text(words)
