from PIL import Image, ImageDraw

from app.engines.tesseract_engine import TesseractEngine


def test_tesseract_language_order_isolates_kazakh_from_default_multiscript_ocr(
    monkeypatch,
):
    monkeypatch.setattr(
        TesseractEngine,
        "installed_languages",
        staticmethod(lambda: ["eng", "rus", "chi_sim", "kaz", "kir"]),
    )

    assert TesseractEngine.ocr_language_string() == "rus+eng+kir+chi_sim"


def test_tesseract_language_order_keeps_explicit_kazakh_priority(monkeypatch):
    monkeypatch.setattr(
        TesseractEngine,
        "installed_languages",
        staticmethod(lambda: ["eng", "rus", "chi_sim", "kaz", "kir"]),
    )

    assert (
        TesseractEngine.ocr_language_string_for(("kaz", "rus", "eng"))
        == "kaz+rus+eng"
    )


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
