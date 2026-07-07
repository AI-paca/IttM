from __future__ import annotations

import re

BASE_LANGUAGES = ("rus", "eng")
OPTIONAL_LANGUAGES = ("kaz", "kir", "chi_sim")
REVIEWER_EXTRA_LANGUAGES = ("ell", "equ")
T9_RETRY_LANGUAGES = REVIEWER_EXTRA_LANGUAGES
ISOLATED_LANGUAGES = ("kaz",)
LANGUAGE_SCRIPTS = {
    "eng": "latin",
    "rus": "cyrillic",
    "kaz": "cyrillic",
    "kir": "cyrillic",
    "chi_sim": "cjk",
    "chi_tra": "cjk",
    "ell": "greek",
    "equ": "math",
}
SCRIPT_PATTERNS = {
    "latin": re.compile(r"[A-Za-z]"),
    "cyrillic": re.compile(r"[\u0400-\u04ff]"),
    "cjk": re.compile(r"[\u3400-\u9fff]"),
    "greek": re.compile(r"[\u0370-\u03ff]"),
    "math": re.compile(r"[\u2200-\u22ff+\-*/=<>^_√∫ΣΠπ∞≈≠≤≥]"),
}


def ocr_language_string_for(
    language_priority: tuple[str, ...],
    installed_languages: list[str],
) -> str:
    installed = set(installed_languages)
    languages = [lang for lang in language_priority if lang in installed]
    if len(languages) > 1 and languages[0] not in ISOLATED_LANGUAGES:
        languages = [lang for lang in languages if lang not in ISOLATED_LANGUAGES]
    return "+".join(languages or ["eng"])


def script_counts(text: str) -> dict[str, int]:
    return {script: len(pattern.findall(text)) for script, pattern in SCRIPT_PATTERNS.items()}


def language_script(language: str) -> str:
    return LANGUAGE_SCRIPTS.get(language, "latin")


def normalize_probabilities(
    probabilities: dict[str, float],
) -> dict[str, float]:
    total = sum(max(0.0, probability) for probability in probabilities.values())
    if total <= 0:
        if not probabilities:
            return {}
        fallback = 1.0 / len(probabilities)
        return {language: fallback for language in probabilities}
    return {language: max(0.0, probability) / total for language, probability in probabilities.items()}
