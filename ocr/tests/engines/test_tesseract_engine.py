from PIL import Image, ImageDraw

from app.engines.tesseract_engine import TesseractEngine
from app.recognition.candidates import LanguageCandidateSelector


def test_tesseract_language_order_isolates_kazakh_from_default_multiscript_ocr(
    monkeypatch,
):
    monkeypatch.setattr(
        TesseractEngine,
        "installed_languages",
        staticmethod(lambda: ["eng", "rus", "chi_sim", "kaz", "kir", "ell", "equ"]),
    )

    assert TesseractEngine.ocr_language_string() == "rus+eng+kir+chi_sim"


def test_tesseract_language_order_keeps_explicit_kazakh_priority(monkeypatch):
    monkeypatch.setattr(
        TesseractEngine,
        "installed_languages",
        staticmethod(lambda: ["eng", "rus", "chi_sim", "kaz", "kir", "ell", "equ"]),
    )

    assert TesseractEngine.ocr_language_string_for(("kaz", "rus", "eng")) == "kaz+rus+eng"


def test_tesseract_language_order_falls_back_to_english(monkeypatch):
    monkeypatch.setattr(
        TesseractEngine,
        "installed_languages",
        staticmethod(lambda: []),
    )

    assert TesseractEngine.ocr_language_string() == "eng"


def test_tesseract_instance_uses_profile_language_priority(monkeypatch):
    monkeypatch.setattr(
        TesseractEngine,
        "installed_languages",
        staticmethod(lambda: ["eng", "rus", "chi_sim"]),
    )

    engine = TesseractEngine(language_priority=("chi_sim", "rus", "eng"))

    assert engine.configured_ocr_language_string() == "chi_sim+rus+eng"


def test_tesseract_language_retry_ranks_candidates_from_probabilities(monkeypatch):
    monkeypatch.setattr(
        TesseractEngine,
        "installed_languages",
        staticmethod(lambda: ["eng", "rus", "chi_sim", "kir", "ell", "equ"]),
    )

    engine = TesseractEngine(
        language_priority=("rus", "eng", "kir", "chi_sim"),
        language_retry="t9_small",
    )

    assert engine._language_candidates() == (
        "rus+eng+kir+chi_sim",
        "rus",
        "eng",
        "kir",
        "chi_sim",
        "ell",
        "equ",
    )

    engine._update_language_probabilities("PDF API browser table")
    english_probabilities = engine.language_probabilities()

    assert english_probabilities["eng"] > english_probabilities["rus"]
    assert engine._language_candidates()[1:3] == ("eng", "rus")

    engine._update_language_probabilities("учебный план дисциплина кафедра")
    engine._update_language_probabilities("направление подготовки зачет экзамен")
    russian_probabilities = engine.language_probabilities()

    assert russian_probabilities["rus"] > russian_probabilities["eng"]
    assert engine._language_candidates()[1] == "rus"


def test_tesseract_language_retry_uses_local_neighbor_context(monkeypatch):
    monkeypatch.setattr(
        TesseractEngine,
        "installed_languages",
        staticmethod(lambda: ["eng", "rus", "chi_sim", "kir", "ell", "equ"]),
    )

    engine = TesseractEngine(
        language_priority=("rus", "eng", "kir", "chi_sim"),
        language_retry="t9_small",
    )

    with engine.language_context("x² + y² = 1"):
        assert engine._language_candidates()[1] == "equ"

    with engine.language_context("Παπαδόπουλος"):
        assert engine._language_candidates()[1] == "ell"


def test_tesseract_language_retry_learns_numeric_rows(monkeypatch):
    monkeypatch.setattr(
        TesseractEngine,
        "installed_languages",
        staticmethod(lambda: ["eng", "rus", "chi_sim", "kir", "ell", "equ"]),
    )

    engine = TesseractEngine(
        language_priority=("rus", "eng", "kir", "chi_sim"),
        language_retry="t9_small",
    )
    initial = engine.language_probabilities()

    engine._update_language_probabilities("36 72 4 3")
    engine._update_language_probabilities("108 40 20 32")

    assert engine.language_probabilities()["equ"] > initial["equ"]
    assert engine._language_candidates()[1] == "equ"


def test_tesseract_language_retry_does_not_treat_a_date_as_math(monkeypatch):
    monkeypatch.setattr(
        TesseractEngine,
        "installed_languages",
        staticmethod(lambda: ["eng", "rus", "chi_sim", "kir", "ell", "equ"]),
    )

    engine = TesseractEngine(
        language_priority=("rus", "eng", "kir", "chi_sim"),
        language_retry="t9_small",
    )

    engine._update_language_probabilities(
        "Учебный план утвержден 19.09.2017 года",
    )

    assert engine._language_candidates()[1] == "rus"


def test_tesseract_language_retry_splays_successful_single_candidate(monkeypatch):
    monkeypatch.setattr(
        TesseractEngine,
        "installed_languages",
        staticmethod(lambda: ["eng", "rus", "chi_sim", "kir", "ell", "equ"]),
    )

    engine = TesseractEngine(
        language_priority=("rus", "eng", "kir", "chi_sim"),
        language_retry="t9_small",
    )

    engine._observe_language_candidate("eng", "PDF API browser table")
    assert engine._language_candidates()[1] == "eng"

    engine._observe_language_candidate("rus", "учебный план кафедра")
    assert engine._language_candidates()[1] == "rus"


def test_tesseract_t9_retry_considers_math_and_greek_candidates(monkeypatch):
    monkeypatch.setattr(
        TesseractEngine,
        "installed_languages",
        staticmethod(lambda: ["eng", "rus", "chi_sim", "kir", "ell", "equ"]),
    )

    engine = TesseractEngine(
        language_priority=("rus", "eng", "kir", "chi_sim"),
        language_retry="t9_small",
    )

    assert "ell" in engine._language_candidates()
    assert "equ" in engine._language_candidates()


def test_tesseract_t9_candidate_selection_can_escape_noisy_primary():
    engine = TesseractEngine(language_retry="t9_small")

    lang, text = engine._select_text_candidate(
        [
            ("rus+eng+chi_sim", 'ЖЕ" 27 (( J /2 үт, a // Я а от @ 8-3 ae ©'),
            ("rus", "ПРОКРАСТИНАЦИЯ составь максимальное количество слов"),
            ("eng", "Directum www.directum.ru"),
        ]
    )

    assert lang == "rus"
    assert text == "ПРОКРАСТИНАЦИЯ составь максимальное количество слов"


def test_tesseract_t9_candidate_selection_keeps_plausible_primary():
    engine = TesseractEngine(language_retry="t9_small")

    lang, text = engine._select_text_candidate(
        [
            ("rus+eng", "Directum www.directum.ru"),
            ("rus", "Оирестит www directum ru"),
        ]
    )

    assert lang == "rus+eng"
    assert text == "Directum www.directum.ru"


def test_language_candidate_selector_uses_injected_reviewer():
    class PreferMarkedReviewer:
        def text_score(self, text: str) -> float:
            return 100.0 if "keep-me" in text else 1.0

        def text_noise_ratio(self, text: str) -> float:
            return 0.0

        def word_quality(self, words: list[dict]) -> float:
            return 0.0

        def should_retry_word_candidates(self, words: list[dict]) -> bool:
            return True

        def words_to_text(self, words: list[dict]) -> str:
            return " ".join(str(word.get("text", "")) for word in words)

    selector = LanguageCandidateSelector(
        language_priority=("rus", "eng"),
        language_retry="t9_small",
        installed_languages=lambda: ["rus", "eng"],
        reviewer=PreferMarkedReviewer(),
    )

    lang, text = selector.select_text_candidate(
        [
            ("rus+eng", "ordinary text"),
            ("rus", "keep-me"),
        ]
    )

    assert lang == "rus"
    assert text == "keep-me"


def test_tesseract_t9_candidate_selection_rejects_cjk_symbol_noise():
    engine = TesseractEngine(language_retry="t9_small")

    lang, text = engine._select_text_candidate(
        [
            ("rus+eng+chi_sim", "6 1 ч 3 9-3 5-2 - 4"),
            ("chi_sim", "全 0 凡 几 了 0 [ 有 民 上 / 才 9一忆 国 要 省"),
        ]
    )

    assert lang == "rus+eng+chi_sim"
    assert text == "6 1 ч 3 9-3 5-2 - 4"


def test_tesseract_t9_word_retry_uses_quality_not_only_count():
    noisy_words = [
        {"text": "ЖЕ\"", "bbox": (0, 0, 10, 10), "conf": 42},
        {"text": "(((", "bbox": (12, 0, 20, 10), "conf": 42},
        {"text": "///", "bbox": (22, 0, 30, 10), "conf": 42},
        {"text": "©", "bbox": (32, 0, 40, 10), "conf": 42},
    ]
    clean_words = [
        {"text": "Directum", "bbox": (0, 0, 10, 10), "conf": 42},
        {"text": "www.directum.ru", "bbox": (12, 0, 20, 10), "conf": 42},
        {"text": "ПРОКРАСТИНАЦИЯ", "bbox": (22, 0, 30, 10), "conf": 42},
    ]

    assert TesseractEngine._should_retry_word_languages(noisy_words) is True
    assert TesseractEngine._should_retry_word_languages(clean_words) is False


def test_tesseract_t9_word_retry_selects_one_language_hypothesis():
    engine = TesseractEngine(language_retry="t9_small")
    primary_words = [
        {"text": "ПРОКРАСТИНАЦИЯ", "bbox": (0, 0, 100, 10), "conf": 70},
        {"text": "составь", "bbox": (0, 12, 40, 22), "conf": 70},
        {"text": "слова", "bbox": (45, 12, 80, 22), "conf": 70},
    ]
    cjk_noise = [
        {"text": "全", "bbox": (0, 0, 10, 10), "conf": 80},
        {"text": "凡", "bbox": (12, 0, 22, 10), "conf": 80},
        {"text": "国", "bbox": (24, 0, 34, 10), "conf": 80},
    ]

    selected = engine._select_word_candidates(
        [
            ("rus+eng+chi_sim", primary_words),
            ("chi_sim", cjk_noise),
        ]
    )

    assert selected == primary_words


def test_edge_ink_detector_requires_all_sides():
    edge_to_edge = Image.new("RGB", (200, 100), "white")
    centered = Image.new("RGB", (200, 100), "white")
    try:
        edge_draw = ImageDraw.Draw(edge_to_edge)
        edge_draw.rectangle((0, 0, 199, 99), outline="black", width=8)

        centered_draw = ImageDraw.Draw(centered)
        centered_draw.rectangle((60, 30, 140, 70), fill="black")

        assert TesseractEngine._edge_ink_touches_all_sides(edge_to_edge) is True
        assert TesseractEngine._edge_ink_touches_all_sides(centered) is False
    finally:
        edge_to_edge.close()
        centered.close()
