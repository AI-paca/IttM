from app.recognition.language_agenda import LanguageAgenda
from app.recognition.languages import BASE_LANGUAGES, OPTIONAL_LANGUAGES, REVIEWER_EXTRA_LANGUAGES
from app.recognition.math_language import score_math_language


def test_score_math_language_bonus_for_formula_with_symbols() -> None:
    scores = score_math_language("x² + y² = 1")

    assert scores["equ"] > 0.0
    assert scores["equ"] > scores["ell"]


def test_score_math_language_bonus_for_units() -> None:
    scores = score_math_language("125.5 kg / m²")

    assert scores["equ"] > scores["ell"]
    assert scores["equ"] > 2.0


def test_score_math_language_accepts_attached_percent_unit() -> None:
    scores = score_math_language("50%")

    assert scores["equ"] > 0.0
    assert scores["ell"] == 0.0


def test_score_math_language_does_not_treat_product_suffix_as_seconds() -> None:
    scores = score_math_language("HP Laptop 15440021s")

    assert scores["equ"] == 0.0
    assert scores["ell"] == 0.0


def test_score_math_language_bonus_for_greek_formula() -> None:
    scores = score_math_language("Σx_i = 2πr")

    assert scores["ell"] > 0.0
    assert scores["equ"] > 0.0


def test_score_math_language_reduces_plain_dates() -> None:
    scores = score_math_language("19.09.2017")

    assert scores["equ"] < 1.0
    assert scores["ell"] == 0.0


def test_language_agenda_equ_receives_sidecar_bonus_in_formula_context():
    agenda = LanguageAgenda(
        language_priority=(*BASE_LANGUAGES, *OPTIONAL_LANGUAGES, *REVIEWER_EXTRA_LANGUAGES),
        language_retry="t9_small",
        installed_languages=lambda: ["eng", "rus", "chi_sim", "ell", "equ"],
    )

    evidence = agenda.language_evidence("x + y = z")

    assert evidence["equ"] > 0.0
    assert evidence["equ"] > max(
        evidence["eng"],
        evidence["rus"],
        evidence["chi_sim"],
    )
