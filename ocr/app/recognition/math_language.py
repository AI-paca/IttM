from __future__ import annotations

import re

_MATH_SYMBOLS = re.compile(r"[+\-*/=<>≤≥≈≠±×÷√∫∑∏∂∇∞∝∈∉∀∃⊕⊖⊗⊘⊙]")
_MATH_OPERATOR = re.compile(r"[+\-*/=<>≤≥≈≠]")
_SUPERSCRIPT = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]")
_FRACTION = re.compile(r"\b\d+\s*/\s*\d+\b")
_VARIABLE = re.compile(r"\b[A-Za-z\u0370-\u03ff]\b", re.UNICODE)
_NUMBER_TOKEN = re.compile(r"\b\d+(?:[.,]\d+)?\b")
_DATE_LIKE = re.compile(r"^\s*\d{1,4}([./-])\d{1,2}\1\d{1,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*$")
_GREEK_LETTERS = re.compile(r"[\u0370-\u03ff]", re.UNICODE)
_SPACED_UNIT = (
    r"(?:"
    r"[kMGT]?m|cm|mm|nm|um|µm|km|ft|in|lb|oz|mg|g|kg|t|"
    r"s|ms|µs|us|ns|min|h|A|kA|mA|V|mV|kV|W|kW|MW|GW|J|N|Pa|kPa|"
    r"MPa|bar|psi|Hz|kHz|MHz|GHz|°C|°F|°|mol|Ω|%|K|rad|sr|m²|m3|l|L|mol/L"
    r")\b"
)
_ATTACHED_UNIT = (
    r"(?:"
    r"cm|mm|nm|um|µm|km|ft|in|lb|oz|mg|kg|ms|µs|us|ns|min|kA|mA|mV|kV|"
    r"kW|MW|GW|Pa|kPa|MPa|bar|psi|Hz|kHz|MHz|GHz|°C|°F|°|mol|Ω|%|K|"
    r"rad|sr|m²|m3|mol/L"
    r")"
)
_PER_UNIT = r"(?:[kMGT]?(?:m|s|A|V|W|Hz)|mol|%|rad|sr|m²|m3)"
_MEASUREMENT_UNIT = re.compile(
    rf"(?<![\w.])\d+(?:[.,]\d+)?(?:\s+{_SPACED_UNIT}|{_ATTACHED_UNIT})(?:\s*/\s*{_PER_UNIT})?(?!\w)",
    re.IGNORECASE,
)


def _has_unit_context(text: str) -> int:
    return len(_MEASUREMENT_UNIT.findall(text))


def score_math_language(text: str) -> dict[str, float]:
    """Return lightweight math/Greek bonus scores for fallback language priors.

    Keys are language codes used by T9 fallback configuration (`equ`, `ell`).
    """

    if not text.strip():
        return {"equ": 0.0, "ell": 0.0}

    normalized = text.strip()
    math_symbols = len(_MATH_SYMBOLS.findall(normalized))
    operators = len(_MATH_OPERATOR.findall(normalized))
    superscripts = len(_SUPERSCRIPT.findall(normalized))
    fractions = len(_FRACTION.findall(normalized))
    variables = len(_VARIABLE.findall(normalized))
    digits = len(_NUMBER_TOKEN.findall(normalized))
    greek_letters = len(_GREEK_LETTERS.findall(normalized))
    unit_count = _has_unit_context(normalized)

    is_date_like = bool(_DATE_LIKE.fullmatch(normalized))

    has_formula_signature = not is_date_like and operators > 0 and (variables > 0 or digits > 0)

    equ_score = 0.0
    ell_score = 0.0

    if has_formula_signature:
        equ_score += 2.4
        equ_score += min(2.5, operators * 0.55)
    if math_symbols:
        equ_score += min(1.8, math_symbols * 0.25)
        if greek_letters:
            ell_score += 0.7
    if unit_count:
        equ_score += min(2.2, unit_count * 1.0)
    if fractions:
        equ_score += min(1.8, fractions * 1.2)
    if superscripts:
        equ_score += min(1.2, superscripts * 0.4)

    if greek_letters:
        ell_score += min(2.4, greek_letters * 0.35)
        if has_formula_signature or unit_count:
            ell_score += 0.8
            equ_score += 0.4

    if is_date_like and not has_formula_signature:
        equ_score *= 0.15

    return {
        "equ": equ_score,
        "ell": ell_score,
    }
