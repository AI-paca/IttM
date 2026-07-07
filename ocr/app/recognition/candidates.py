from __future__ import annotations

from collections.abc import Callable
import re

from app.recognition.language_agenda import LanguageAgenda, split_language_group
from app.recognition.reviewer import (
    HeuristicCandidateReviewer,
    RecognitionCandidateReviewer,
)


class LanguageCandidateSelector:
    def __init__(
        self,
        *,
        language_priority: tuple[str, ...] | None,
        language_retry: str,
        installed_languages: Callable[[], list[str]],
        reviewer: RecognitionCandidateReviewer | None = None,
    ) -> None:
        self.language_priority = language_priority
        self.language_retry = language_retry
        self._installed_languages = installed_languages
        self.reviewer = reviewer or HeuristicCandidateReviewer()
        self.agenda = LanguageAgenda(
            language_priority=language_priority,
            language_retry=language_retry,
            installed_languages=installed_languages,
        )

    def configured_ocr_language_string(self) -> str:
        return self.agenda.configured_ocr_language_string()

    def single_language_candidates(self) -> tuple[str, ...]:
        return self.agenda.single_language_candidates()

    def initial_language_probabilities(self) -> dict[str, float]:
        return self.agenda.initial_language_probabilities()

    def language_probabilities(self) -> dict[str, float]:
        return self.agenda.probabilities()

    def language_evidence(self, text: str) -> dict[str, float]:
        return self.agenda.language_evidence(text)

    def update_language_probabilities(self, text: str) -> None:
        self.agenda.observe_text(text)

    def observe_language_candidate(self, lang: str | None, text: str) -> None:
        self.agenda.observe_candidate(lang, text)

    def ranked_single_language_candidates(
        self,
        context: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        return self.agenda.ranked_single_language_candidates(context)

    def language_candidates(
        self,
        context: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        return self.agenda.language_candidates(context)

    def candidate_prior_score(
        self,
        lang: str | None,
        context: tuple[str, ...] = (),
    ) -> float:
        return self.agenda.candidate_prior_score(lang, context)

    def candidate_evidence_score(
        self,
        lang: str | None,
        text: str,
    ) -> float:
        return self.agenda.candidate_evidence_score(lang, text)

    @staticmethod
    def script_candidate_penalty(lang: str | None, text: str) -> float:
        languages = list(split_language_group(lang))
        if languages != ["chi_sim"]:
            return 0.0

        cjk_tokens = re.findall(r"[\u3400-\u9fff]+", text)
        cjk_chars = sum(len(token) for token in cjk_tokens)
        if cjk_chars < 2:
            return 0.0

        single_char_ratio = sum(len(token) == 1 for token in cjk_tokens) / max(
            1,
            len(cjk_tokens),
        )
        multi_char_ratio = sum(len(token) >= 2 for token in cjk_tokens) / max(
            1,
            len(cjk_tokens),
        )
        non_space = max(1, sum(not character.isspace() for character in text))
        ascii_noise = sum(
            character.isascii()
            and not character.isspace()
            and not character.isalpha()
            for character in text
        )
        ascii_noise_ratio = ascii_noise / non_space

        if (
            single_char_ratio >= 0.62
            and multi_char_ratio < 0.25
        ) or ascii_noise_ratio >= 0.28:
            return 45.0
        return 0.0

    def text_candidate_score(
        self,
        text: str,
        lang: str | None,
        context: tuple[str, ...] = (),
    ) -> float:
        if not text.strip():
            return -1.0
        return (
            self.reviewer.text_score(text)
            + (self.candidate_prior_score(lang, context) * 1.5)
            + (self.candidate_evidence_score(lang, text) * 2.0)
            - self.script_candidate_penalty(lang, text)
        )

    def select_text_candidate(
        self,
        candidates: list[tuple[str, str]],
        context: tuple[str, ...] = (),
    ) -> tuple[str, str]:
        if not candidates:
            return "", ""

        primary_lang, primary_text = candidates[0]
        scored = [
            (lang, text, self.text_candidate_score(text, lang, context))
            for lang, text in candidates
        ]
        best_lang, best_text, best_score = max(
            scored,
            key=lambda candidate: candidate[2],
        )
        primary_score = scored[0][2]
        primary_noise = self.reviewer.text_noise_ratio(primary_text)
        primary_margin = max(6.0, primary_score * 0.12)
        if primary_noise >= 0.18 or primary_score < 18.0:
            primary_margin = 0.0
        if (
            best_lang != primary_lang
            and primary_text.strip()
            and best_score < primary_score + primary_margin
        ):
            return primary_lang, primary_text
        return best_lang, best_text

    def select_word_candidates(
        self,
        candidates: list[tuple[str, list[dict]]],
        context: tuple[str, ...] = (),
    ) -> list[dict]:
        return self.select_word_candidate(candidates, context)[1]

    def select_word_candidate(
        self,
        candidates: list[tuple[str, list[dict]]],
        context: tuple[str, ...] = (),
    ) -> tuple[str, list[dict]]:
        if not candidates:
            return "", []

        primary_lang, primary_words = candidates[0]
        primary_text = self.reviewer.words_to_text(primary_words)
        scored = []
        for lang, words in candidates:
            text = self.reviewer.words_to_text(words)
            score = (
                self.text_candidate_score(text, lang, context)
                + self.reviewer.word_quality(words)
                + min(8.0, len(words) * 0.25)
            )
            scored.append((lang, words, text, score))

        best_lang, best_words, _, best_score = max(
            scored,
            key=lambda candidate: candidate[3],
        )
        primary_score = scored[0][3]
        primary_noise = self.reviewer.text_noise_ratio(primary_text)
        primary_margin = max(8.0, primary_score * 0.15)
        if primary_noise >= 0.18 or primary_score < 18.0:
            primary_margin = 0.0
        if (
            best_lang != primary_lang
            and primary_words
            and best_score < primary_score + primary_margin
        ):
            return primary_lang, primary_words
        return best_lang, best_words
