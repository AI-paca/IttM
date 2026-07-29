from PIL import Image, ImageDraw

from app.engines.tesseract_engine import TesseractEngine
from app.recognition.candidates import LanguageCandidateSelector
from app.recognition.quality import (
    code_path_quality_score,
    looks_like_mixed_script_ocr_noise,
)


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


def test_tesseract_language_retry_learns_single_numeric_cells(monkeypatch):
    monkeypatch.setattr(
        TesseractEngine,
        "installed_languages",
        staticmethod(lambda: ["eng", "rus", "chi_sim", "kir", "ell", "equ"]),
    )

    engine = TesseractEngine(
        language_priority=("rus", "eng", "kir", "chi_sim", "equ"),
        language_retry="t9_small",
    )
    initial = engine.language_probabilities()

    engine._update_language_probabilities("5")

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


def test_tesseract_language_retry_does_not_promote_by_recency(monkeypatch):
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
    assert engine._language_candidates()[1] == "eng"


def test_tesseract_language_retry_tracks_empty_without_ocr_language(monkeypatch):
    monkeypatch.setattr(
        TesseractEngine,
        "installed_languages",
        staticmethod(lambda: ["eng", "rus", "chi_sim", "kir", "ell", "equ"]),
    )

    engine = TesseractEngine(
        language_priority=("rus", "eng", "kir", "chi_sim"),
        language_retry="t9_small",
    )
    initial_empty = engine.language_probabilities()["empty"]

    assert "empty" in engine.language_probabilities()
    assert "empty" not in engine._language_candidates()

    engine._observe_language_candidate("empty", "")

    assert engine.language_probabilities()["empty"] > initial_empty
    assert "empty" not in engine._language_candidates()


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


def test_tesseract_t9_candidate_selection_prefers_clean_code_path_leaf():
    engine = TesseractEngine(language_retry="t9_small")

    lang, text = engine._select_text_candidate(
        [
            ("rus+eng", r"resolve pipeline profile в осг/арр/р1ре\1пе_соп?19.ру:"),
            ("eng", "resolve pipeline profile B ocr/app/pipeline_config.py:"),
        ]
    )

    assert lang == "eng"
    assert text == "resolve pipeline profile B ocr/app/pipeline_config.py:"


def test_tesseract_t9_does_not_score_numeric_slashes_as_code_paths():
    text = "Формат 90x60/32. Заказ № 936/1. Технический регламент 007/2011."

    assert code_path_quality_score(text) == 0.0


def test_tesseract_t9_does_not_penalize_transliterated_hyphenated_words():
    text = "Издание для 21-yй школы, 1-kypылыc, 705-бeлme, 7-kaбat."

    assert code_path_quality_score(text) == 0.0


def test_tesseract_t9_candidate_selection_keeps_mixed_language_sentence():
    engine = TesseractEngine(language_retry="t9_small")

    lang, text = engine._select_text_candidate(
        [
            ("rus+eng", "1. Если pipeline profile не задан - взять default для engine type (backend auto standard /"),
            ("eng", "1. Ecnm pipeline profile He 3agaH - B3aTb default gna engine type (backend auto standard /"),
        ]
    )

    assert lang == "rus+eng"
    assert text == "1. Если pipeline profile не задан - взять default для engine type (backend auto standard /"


def test_tesseract_t9_keeps_cyrillic_prose_with_foreign_parenthetical():
    engine = TesseractEngine(language_retry="t9_small")
    primary = (
        "Артикль — служебная часть речи, которая является грамматическим "
        "признаком существительного. В немецком языке он изменяется по родам "
        "и падежам (DER ARTIKEL)."
    )
    transliteration = (
        "Artikl — sluzhebnaya chast rechi, kotoraya yavlyaetsya "
        "grammaticheskim priznakom (DER ARTIKEL)."
    )

    assert not looks_like_mixed_script_ocr_noise(primary)
    lang, text = engine._select_text_candidate(
        [
            ("rus+eng", primary),
            ("eng", transliteration),
        ]
    )

    assert lang == "rus+eng"
    assert text == primary


def test_tesseract_t9_candidate_selection_rejects_latin_tech_cyrillic_noise():
    engine = TesseractEngine(language_retry="t9_small")

    lang, text = engine._select_text_candidate(
        [
            ("rus+eng", "backend tesseract standard / БасКепа easyocr standard)."),
            ("eng", "backend tesseract standard /backend easyocr standard)."),
        ]
    )

    assert lang == "eng"
    assert text == "backend tesseract standard /backend easyocr standard)."


def test_tesseract_t9_candidate_selection_rejects_cyrillic_tech_confusables():
    engine = TesseractEngine(language_retry="t9_small")

    lang, text = engine._select_text_candidate(
        [
            ("rus+eng", "© backend tesseract standard / БасКепа easyocr standard)."),
            ("eng", "9 backend tesseract standard /backend easyocr standard)."),
            ("rus", "© БасКепа тес5егас? 5Хапдага / БасКепа еазуосг 5Хапдага)."),
        ]
    )

    assert lang == "eng"
    assert text == "9 backend tesseract standard /backend easyocr standard)."


def test_tesseract_t9_empty_candidate_can_replace_punctuation_noise():
    engine = TesseractEngine(language_retry="t9_small")

    lang, text = engine._select_text_candidate(
        [
            ("rus+eng", "..."),
            ("eng", "|_';"),
        ]
    )

    assert lang == "empty"
    assert text == ""


def test_tesseract_t9_empty_candidate_does_not_hide_single_digit():
    engine = TesseractEngine(language_retry="t9_small")

    lang, text = engine._select_text_candidate(
        [
            ("rus+eng", "5"),
            ("eng", ""),
        ]
    )

    assert lang == "rus+eng"
    assert text == "5"


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
        {"text": 'ЖЕ"', "bbox": (0, 0, 10, 10), "conf": 42},
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


def test_tesseract_t9_word_retry_detects_latin_table_with_cyrillic_ocr_noise():
    words = [
        {"text": "ASUS", "bbox": (0, 0, 30, 10), "conf": 60},
        {"text": "Vivobook", "bbox": (32, 0, 90, 10), "conf": 60},
        {"text": "15", "bbox": (92, 0, 110, 10), "conf": 60},
        {"text": "АЗИЗ", "bbox": (112, 0, 150, 10), "conf": 60},
        {"text": "Laptop", "bbox": (0, 12, 40, 22), "conf": 60},
        {"text": "Intel", "bbox": (42, 12, 72, 22), "conf": 60},
        {"text": "Core", "bbox": (74, 12, 104, 22), "conf": 60},
        {"text": "SSD", "bbox": (106, 12, 134, 22), "conf": 60},
        {"text": "ВОВ", "bbox": (136, 12, 166, 22), "conf": 60},
    ]

    assert looks_like_mixed_script_ocr_noise(TesseractEngine._words_to_text(words))
    assert TesseractEngine._should_retry_word_languages(words) is True


def test_tesseract_t9_word_retry_keeps_balanced_real_mixed_language_text():
    words = [
        {"text": "Directum", "bbox": (0, 0, 45, 10), "conf": 60},
        {"text": "www.directum.ru", "bbox": (48, 0, 130, 10), "conf": 60},
        {"text": "ПРОКРАСТИНАЦИЯ", "bbox": (0, 12, 110, 22), "conf": 60},
        {"text": "составь", "bbox": (112, 12, 160, 22), "conf": 60},
        {"text": "слова", "bbox": (162, 12, 198, 22), "conf": 60},
    ]

    assert not looks_like_mixed_script_ocr_noise(TesseractEngine._words_to_text(words))
    assert TesseractEngine._should_retry_word_languages(words) is False


def test_tesseract_t9_candidate_selection_escapes_latin_dominant_noisy_primary():
    engine = TesseractEngine(language_retry="t9_small")
    noisy_primary = [
        {"text": "ASUS", "bbox": (0, 0, 30, 10), "conf": 60},
        {"text": "Vivobook", "bbox": (32, 0, 90, 10), "conf": 60},
        {"text": "15", "bbox": (92, 0, 110, 10), "conf": 60},
        {"text": "АЗИЗ", "bbox": (112, 0, 150, 10), "conf": 60},
        {"text": "Laptop", "bbox": (0, 12, 40, 22), "conf": 60},
        {"text": "Intel", "bbox": (42, 12, 72, 22), "conf": 60},
        {"text": "Core", "bbox": (74, 12, 104, 22), "conf": 60},
        {"text": "SSD", "bbox": (106, 12, 134, 22), "conf": 60},
        {"text": "ВОВ", "bbox": (136, 12, 166, 22), "conf": 60},
    ]
    english = [
        {**word, "text": text}
        for word, text in zip(
            noisy_primary,
            ("ASUS", "Vivobook", "15", "Laptop", "Intel", "Core", "i5", "SSD", "8GB"),
        )
    ]

    lang, words = engine._select_word_candidate(
        [
            ("rus+eng+chi_sim", noisy_primary),
            ("eng", english),
        ]
    )

    assert lang == "eng"
    assert words == english


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
