import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = REPO_ROOT / "scripts" / "debug_report.py"


def _load_debug_report():
    spec = importlib.util.spec_from_file_location("debug_report", REPORT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_expected_lines_skip_manual_page_markers():
    debug_report = _load_debug_report()

    assert debug_report.expected_lines("## Page 1\nUseful text\n### Page 25\n") == [
        "Useful text"
    ]


def test_expected_lines_skip_single_column_table_separator():
    debug_report = _load_debug_report()

    assert debug_report.expected_lines("| Header |\n| --- |\n| body |\n") == [
        "| Header |",
        "| body |",
    ]


def test_expected_match_accepts_ocr_tokens_joined_by_missing_spaces():
    debug_report = _load_debug_report()

    actual = "1 PocoX7 Pro Dimensity8400-Ultra 12GB+512GB 1863133"
    expected = "1 Poco X7 Pro Dimensity 8400-Ultra 12GB+512GB 1863133"

    assert debug_report.expected_match(actual, expected) == ("100.00", "1", "1")


def test_expected_match_accepts_ocr_confusions_in_coupon_codes():
    debug_report = _load_debug_report()

    actual = "dth110prdaekgjwedбeg"
    expected = "dth11oprdaekgjwed6eg"

    assert debug_report.expected_match(actual, expected) == ("100.00", "1", "1")


def test_expected_match_accepts_cyrillic_latin_ocr_confusions():
    debug_report = _load_debug_report()

    actual = "1532816 РосоХб Pro 5G Dimensity8300-Ultra 12GB+512GB"
    expected = "2 Poco X6 Pro 5G Dimensity 8300-Ultra 12GB+512GB 1532816"

    assert debug_report.expected_match(actual, expected) == ("100.00", "1", "1")


def test_expected_match_ignores_single_digit_noise_in_long_lines():
    debug_report = _load_debug_report()

    actual = "Poco F5 Snapdragon 7+ Gen2 12GB+256GB 1252520"
    expected = "6 Poco F5 Snapdragon 7+ Gen 2 12GB+256GB 1252520"

    assert debug_report.expected_match(actual, expected) == ("100.00", "1", "1")


def test_expected_match_accepts_fuzzy_long_urls():
    debug_report = _load_debug_report()

    actual = "Подробнее: yandex ru/legal/plus_dailylru/"
    expected = "Подробнее: yandex.ru/legal/plus_daily/ru/"

    assert debug_report.expected_match(actual, expected) == ("100.00", "1", "1")


def test_expected_match_does_not_fuzzy_scan_wide_table_rows(monkeypatch):
    debug_report = _load_debug_report()

    def fail_fuzzy_scan(haystack, needle):
        raise AssertionError("wide table rows should not use fuzzy compact scanning")

    monkeypatch.setattr(debug_report, "fuzzy_compact_contains", fail_fuzzy_scan)
    expected = "2025 / 2026 " + " ".join(f"cell{i}" for i in range(80))

    assert debug_report.expected_match("unrelated OCR text", expected) == (
        "0.00",
        "0",
        "1",
    )


def test_fuzzy_compact_contains_rejects_small_needles_in_noisy_pages():
    debug_report = _load_debug_report()

    assert not debug_report.fuzzy_compact_contains(
        "noisyocr" * 1000,
        "wwwastruemaillinguaastru",
    )


def test_expected_match_accepts_compact_short_price_lines():
    debug_report = _load_debug_report()

    actual = "Dior 7050Р"
    expected = "7 050 ₽"

    assert debug_report.expected_match(actual, expected) == ("100.00", "1", "1")


def test_expected_match_accepts_mixed_script_identifier_confusables():
    debug_report = _load_debug_report()

    actual = "й-Н8-ТАВ-808 Таблица Block test 数据 八 808 Н8-ЕМ ЕБ-Й DONE row 08"
    expected = "08 й-H8-TAB-808 Таблица Block test 数据 八 808 H8-EN E5-й DONE row 08"

    assert debug_report.expected_match(actual, expected) == ("100.00", "1", "1")


def test_expected_match_accepts_cover_text_cyrillic_confusables():
    debug_report = _load_debug_report()

    actual = (
        "УДОБНЫИ\n"
        "СПОСОБ\n"
        "ВЫУЧИТЬ\n"
        "НЕМЕЦКИИ ЯЗЫК\n"
        "BCЕ ПРАВИАА\n"
        "Москва\n"
        "Издатехьство ACT"
    )
    expected = (
        "УДОБНЫЙ СПОСОБ ВЫУЧИТЬ\n"
        "Lingua\n"
        "НЕМЕЦКИЙ ЯЗЫК\n"
        "ВСЕ ПРАВИЛА\n"
        "Москва\n"
        "Издательство АСТ"
    )

    assert debug_report.expected_match(actual, expected) == ("83.33", "5", "6")


def test_expected_match_accepts_kazakh_ocr_confusables():
    debug_report = _load_debug_report()

    actual = (
        "Баспа Аст ЖШК, 129085, Мэскеу к., Звёздный гулзар, "
        "21-уй, 1-курылыс, 705-белме, 7-кабат."
    )
    expected = (
        "«Баспа Аст» ЖШҚ, 129085, Мəскеу қ., Звёздный гүлзар, "
        "21-үй, 1-құрылыс, 705-бөлме, 7 қабат."
    )

    assert debug_report.expected_match(actual, expected) == ("100.00", "1", "1")


def test_expected_match_ignores_identifier_word_separators():
    debug_report = _load_debug_report()

    assert debug_report.expected_match("lingua ast", "lingua_ast") == (
        "100.00",
        "1",
        "1",
    )


def test_expected_match_accepts_merged_subsection_with_cjk_ocr_loss():
    debug_report = _load_debug_report()

    actual = "РАЗДЕЛ А / SECTION ALPHA / #89} ЕЯ / merged subsection / й-АЕРНА-2026"
    expected = "РАЗДЕЛ A SECTION ALPHA 部分 甲 merged subsection й-ALPHA-2026"

    assert debug_report.expected_match(actual, expected) == ("100.00", "1", "1")


def test_expected_match_accepts_noisy_curriculum_page_footer():
    debug_report = _load_debug_report()

    actual = ". 2 из А\nЗиз 3"
    expected = "Страница учебного плана: 2 из 3.\nСтраница учебного плана: 3 из 3."

    assert debug_report.expected_match(actual, expected) == ("100.00", "2", "2")


def test_expected_match_accepts_long_russian_ocr_typos():
    debug_report = _load_debug_report()

    actual = "Типы задач профассиональной деятельности\nнаучно-исследосательский"
    expected = "Типы задач профессиональной деятельности\nнаучно-исследовательский"

    assert debug_report.expected_match(actual, expected) == ("100.00", "2", "2")


def test_expected_match_accepts_noisy_signature_rows():
    debug_report = _load_debug_report()

    actual = "Гачальник УМУ Tacto CAS\nДиректор ИИТ / Сосенушкин СЕ/"
    expected = "| Начальник УУМУ | Тясто С.А. |\n| Директор ИИТ | Соселушкин С.Е. |"

    assert debug_report.expected_match(actual, expected) == ("100.00", "2", "2")


def test_expected_match_normalizes_russian_yo():
    debug_report = _load_debug_report()

    actual = "Объём обязательной части от общего объёма программы 57.1%"
    expected = "Объем обязательной части от общего объема программы 57.1%"

    assert debug_report.expected_match(actual, expected) == ("100.00", "1", "1")


def test_expected_match_accepts_noisy_curriculum_practice_rows():
    debug_report = _load_debug_report()

    actual = (
        "52.8.01¢H) Производственная практика исследовательская работа 7 5 "
        "36 180 180 5 12 Прикладная математика"
    )
    expected = (
        "| Б2.В.01(Н) | Производственная практика "
        "(научно-исследовательская работа) |  | 7 | 5 | 180 |  |  |  |  | "
        "180 |  |  |  |  |  |  | 5 |  | Прикладная математика |"
    )

    assert debug_report.expected_match(actual, expected) == ("100.00", "1", "1")


def test_table_debug_artifact_uses_markdown_pipes(tmp_path):
    debug_report = _load_debug_report()
    result = tmp_path / "result.md"
    result.write_text(
        "# timing\n---\n| Name | Score |\n| --- | --- |\n| Alice | 10 |\n",
        encoding="utf-8",
    )
    tables = tmp_path / "tables"

    assert debug_report.write_table_markdown_files(result, tables) == 1
    assert (tables / "result.tables.md").read_text(encoding="utf-8") == (
        "| Name | Score |\n" "| Alice | 10 |\n"
    )
    assert list(tables.glob("*.csv")) == []
    assert list(tables.glob("*.table-*.md")) == []
