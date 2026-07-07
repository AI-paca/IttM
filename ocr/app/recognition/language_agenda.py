from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from app.recognition.languages import (
    BASE_LANGUAGES,
    OPTIONAL_LANGUAGES,
    REVIEWER_EXTRA_LANGUAGES,
    language_script,
    normalize_probabilities,
    ocr_language_string_for,
    script_counts,
)


def split_language_group(language_group: str | None) -> tuple[str, ...]:
    if not language_group:
        return ()
    return tuple(dict.fromkeys(language for language in language_group.split("+") if language))


def _context_text(context: Iterable[str]) -> str:
    return " ".join(part.strip() for part in context if part.strip())


def numeric_text_evidence(text: str) -> int:
    numeric_tokens = re.findall(
        r"(?<!\w)[+-]?\d+(?:[.,:/-]\d+)*%?(?!\w)",
        text,
    )
    if not numeric_tokens:
        return 0
    digit_count = sum(character.isdigit() for character in text)
    letter_count = sum(character.isalpha() for character in text)
    if len(numeric_tokens) < 2 and digit_count < max(2, letter_count):
        return 0
    return digit_count


class LanguageAgenda:
    """
    Self-adjusting language/type agenda for retry OCR.

    The first OCR attempt still uses the profile's configured language group.
    Retry attempts are single-language candidates ranked by document evidence,
    local left/top context, and a small splay-style recency order. This keeps
    early pages broad, then makes repeated segment OCR cheaper as the document
    reveals its dominant scripts.
    """

    def __init__(
        self,
        *,
        language_priority: tuple[str, ...] | None,
        language_retry: str,
        installed_languages: Callable[[], list[str]],
    ) -> None:
        self.language_priority = language_priority
        self.language_retry = language_retry
        self._installed_languages = installed_languages
        self._probabilities: dict[str, float] | None = None
        self._splay_order: list[str] | None = None

    def configured_ocr_language_string(self) -> str:
        priority = self.language_priority or (BASE_LANGUAGES + OPTIONAL_LANGUAGES)
        return ocr_language_string_for(priority, self._installed_languages())

    def single_language_candidates(self) -> tuple[str, ...]:
        priority = self.language_priority or (BASE_LANGUAGES + OPTIONAL_LANGUAGES)
        if self.language_retry == "t9_small":
            priority = (*priority, *REVIEWER_EXTRA_LANGUAGES)
        primary_languages = [
            language
            for language in ocr_language_string_for(
                priority,
                self._installed_languages(),
            ).split("+")
            if language
        ]
        return tuple(dict.fromkeys(primary_languages or ["eng"]))

    def initial_language_probabilities(self) -> dict[str, float]:
        languages = self.single_language_candidates()
        if not languages:
            return {"eng": 1.0}
        probability = 1.0 / len(languages)
        return {language: probability for language in languages}

    def probabilities(self) -> dict[str, float]:
        if self._probabilities is None:
            self._probabilities = self.initial_language_probabilities()
        return dict(self._probabilities)

    def _ensure_splay_order(self) -> list[str]:
        if self._splay_order is None:
            self._splay_order = list(self.single_language_candidates())
        return self._splay_order

    def language_evidence(self, text: str) -> dict[str, float]:
        priors = self.probabilities()
        counts = script_counts(text)
        numeric_score = numeric_text_evidence(text)
        weighted: dict[str, float] = {}
        for language in priors:
            script = language_script(language)
            score = float(counts.get(script, 0))
            if script == "math" and score:
                score *= 3.0
            if script == "math":
                score += numeric_score
            weighted[language] = score

        if sum(weighted.values()) < 2:
            return {}
        return normalize_probabilities(weighted)

    def context_evidence(self, context: Iterable[str]) -> dict[str, float]:
        return self.language_evidence(_context_text(context))

    def observe_text(self, text: str) -> None:
        evidence = self.language_evidence(text)
        if not evidence:
            return

        priors = self.probabilities()
        updated = {
            language: (priors.get(language, 0.0) * 0.72) + (evidence.get(language, 0.0) * 0.28) for language in priors
        }
        self._probabilities = normalize_probabilities(updated)

    def observe_candidate(self, language_group: str | None, text: str) -> None:
        self.observe_text(text)
        languages = split_language_group(language_group)
        if not languages:
            return

        evidence = self.language_evidence(text)
        if evidence:
            languages = tuple(
                sorted(
                    languages,
                    key=lambda language: -evidence.get(language, 0.0),
                )
            )
        self._promote_languages(languages)

    def _promote_languages(self, languages: tuple[str, ...]) -> None:
        if not languages:
            return
        order = self._ensure_splay_order()
        for language in reversed(languages):
            if language not in order:
                continue
            order.remove(language)
            order.insert(0, language)

    def ranked_single_language_candidates(
        self,
        context: Iterable[str] = (),
    ) -> tuple[str, ...]:
        priors = self.probabilities()
        languages = list(self.single_language_candidates())
        splay_order = {language: index for index, language in enumerate(self._ensure_splay_order())}
        original_order = {language: index for index, language in enumerate(languages)}
        context_scores = self.context_evidence(context)
        splay_size = max(1, len(splay_order))

        def candidate_score(language: str) -> float:
            splay_rank = splay_order.get(language, splay_size)
            splay_score = (splay_size - min(splay_rank, splay_size)) / splay_size
            return (priors.get(language, 0.0) * 10.0) + (context_scores.get(language, 0.0) * 8.0) + (splay_score * 4.0)

        ranked = sorted(
            languages,
            key=lambda language: (
                -candidate_score(language),
                original_order.get(language, 999),
            ),
        )
        if not ranked:
            return ()
        return tuple(dict.fromkeys(ranked))

    def language_candidates(
        self,
        context: Iterable[str] = (),
    ) -> tuple[str, ...]:
        primary = self.configured_ocr_language_string()
        if self.language_retry != "t9_small":
            return (primary,)

        candidates = [
            primary,
            *self.ranked_single_language_candidates(context),
        ]
        return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))

    def candidate_prior_score(
        self,
        language_group: str | None,
        context: Iterable[str] = (),
    ) -> float:
        languages = split_language_group(language_group)
        if not languages:
            return 0.0
        priors = self.probabilities()
        context_scores = self.context_evidence(context)
        return sum(
            priors.get(language, 0.0) + (context_scores.get(language, 0.0) * 0.6) for language in languages
        ) / len(languages)

    def candidate_evidence_score(
        self,
        language_group: str | None,
        text: str,
    ) -> float:
        languages = split_language_group(language_group)
        if not languages:
            return 0.0
        evidence = self.language_evidence(text)
        if not evidence:
            return 0.0
        return sum(evidence.get(language, 0.0) for language in languages)
