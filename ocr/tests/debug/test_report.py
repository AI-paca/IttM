import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
REPORT_PATH = REPO_ROOT / "scripts" / "debug" / "debug_report.py"


def _load_debug_report():
    spec = importlib.util.spec_from_file_location("debug_report", REPORT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_expected_lines_skip_manual_page_markers():
    debug_report = _load_debug_report()

    assert debug_report.expected_lines("## Page 1\nUseful text\n### Page 25\n") == ["Useful text"]


def test_expected_lines_skip_single_column_table_separator():
    debug_report = _load_debug_report()

    assert debug_report.expected_lines("| Header |\n| --- |\n| body |\n") == [
        "| Header |",
        "| body |",
    ]


def test_two_column_reference_is_scored_as_a_table():
    debug_report = _load_debug_report()
    markdown = (
        "| Name | Score |\n"
        "| --- | --- |\n"
        "| Alice | 10 |\n"
        "| Bob | 9 |\n"
    )

    spec = debug_report.table_fixture_spec(markdown)

    assert spec is not None
    assert spec.expected_blocks == 1
    assert spec.expected_rows == 3
    assert spec.expected_cols == 2


def test_table_parser_keeps_escaped_pipes_inside_cells():
    debug_report = _load_debug_report()
    markdown = (
        "| Product | Shop |\n"
        "| --- | --- |\n"
        "| SPF50 \\| Sun | Store |\n"
    )

    tables = debug_report.strict_reference_tables(markdown)
    spec = debug_report.table_fixture_spec(markdown)

    assert tables == [[
        ["Product", "Shop"],
        ["SPF50 | Sun", "Store"],
    ]]
    assert spec is not None
    assert spec.expected_rows == 2
    assert spec.expected_cols == 2
    assert debug_report.markdown_shadow_codes(markdown) == (5, 8)


def test_loose_pipe_reference_rows_are_scored_as_tables():
    debug_report = _load_debug_report()
    markdown = (
        "Title | Link\n"
        "Profiles\n"
        "Profile | Layout | Purpose\n"
        "backend_auto | table_regions | default\n"
        "backend_raw | none | raw OCR\n"
    )

    tables = debug_report.strict_reference_tables(markdown)
    spec = debug_report.table_fixture_spec(markdown)

    assert [debug_report.table_shape(table) for table in tables] == [(3, 3)]
    assert spec is not None
    assert spec.expected_blocks == 1
    assert spec.expected_rows == 3
    assert spec.expected_cols == 3
    assert debug_report.markdown_shadow_codes(markdown) == (
        5,
        5,
        8,
        8,
        8,
        8,
    )


def test_loose_pipe_rows_inside_fenced_code_are_not_tables():
    debug_report = _load_debug_report()
    markdown = (
        "Settings\n\n"
        "```\n"
        "engine_type: auto | tesseract | easyocr\n"
        "pdf_mode: auto | raster\n"
        "```\n\n"
        "Key | Source\n"
        "preprocess | profile\n"
        "layout | selector\n"
    )

    tables = debug_report.strict_reference_tables(markdown)

    assert [debug_report.table_shape(table) for table in tables] == [(3, 2)]
    assert debug_report.markdown_shadow_codes(markdown) == (5, 8, 8)


def test_plain_reference_headings_are_inferred_symmetrically():
    debug_report = _load_debug_report()
    expected = (
        "Policy\n"
        "Trust boundaries\n"
        "Vector | Destination | Risk\n"
        "Browser OCR | worker | low\n"
        "Local OCR | backend | medium\n"
        "Local processing\n"
        "- upload stays local\n"
    )
    actual = (
        "# x\n\n"
        "## x\n\n"
        "| x | x | x |\n"
        "| --- | --- | --- |\n"
        "| x | x | x |\n"
        "| x | x | x |\n\n"
        "## x\n\n"
        "- x\n"
    )

    score = debug_report.scored_expected_match(actual, expected)

    assert score.markdown_grammar_gate == "pass"


def test_identical_plain_reference_structure_scores_one_hundred():
    debug_report = _load_debug_report()
    markdown = (
        "Policy\n"
        "Trust boundaries\n"
        "Local processing\n"
        "- upload stays local\n"
        "- no network request\n"
    )

    score = debug_report.scored_expected_match(markdown, markdown)

    assert score.markdown_grammar_percent == "100.00"
    assert score.success_probability_percent == "100.00"


def test_strict_table_reference_does_not_infer_plain_paragraphs_as_headings():
    debug_report = _load_debug_report()
    expected = (
        "| Title | Logo |\n"
        "| --- | --- |\n\n"
        "Paragraph line one\n"
        "paragraph line two\n\n"
        "- first\n"
        "- second\n"
    )
    actual = (
        "| x x |  |\n"
        "| --- | --- |\n\n"
        "x x x\n\n"
        "- x\n"
        "- x\n"
    )

    score, notes = debug_report.score_markdown_structure(actual, expected)

    assert score == 100.0
    assert "controls=2/2" in notes


def test_large_table_shape_ignores_tiny_actual_prefix_table():
    debug_report = _load_debug_report()
    expected = (
        "| A | B | C |\n"
        "| --- | --- | --- |\n"
        + "\n".join("| 1 | 2 | 3 |" for _ in range(28))
    )
    actual = (
        "| noise |  |\n"
        "| --- | --- |\n\n"
        "| x | x | x |\n"
        "| --- | --- | --- |\n"
        + "\n".join("| x | x | x |" for _ in range(31))
    )

    score, notes = debug_report.score_table_structure(actual, expected)

    assert score is not None
    assert score >= 90.0
    assert "1x2,32x3" in notes


def test_expected_match_keeps_table_cell_score_diagnostic():
    debug_report = _load_debug_report()

    expected = (
        "| A | B | C |\n"
        "| --- | --- | --- |\n"
        "| alpha | beta | gamma |\n"
        "| one | two | three |\n"
    )
    actual = (
        "| A B C | Column 2 | Column 3 |\n"
        "| --- | --- | --- |\n"
        "| alpha | beta | gamma |\n"
        "| one | typo | three |\n"
    )

    table_percent, matched, total = debug_report.strict_table_cell_match(
        actual,
        expected,
    )

    assert table_percent == 55.55555555555556
    assert (matched, total) == (5, 9)
    assert debug_report.expected_match(actual, expected) == ("33.33", "1", "3")


def test_scored_expected_match_rejects_table_token_soup():
    debug_report = _load_debug_report()

    expected = (
        "| Name | Score | Status |\n"
        "| --- | --- | --- |\n"
        "| Alice | Ten | Pass |\n"
        "| Bob | Nine | Fail |\n"
    )
    actual = "Name Score Status Alice Ten Pass Bob Nine Fail"

    score = debug_report.scored_expected_match(actual, expected)

    assert score.text_match_percent == "100.00"
    assert score.markdown_grammar_percent == "0.00"
    assert score.match_percent == "100.00"
    assert score.failure_kind == "markdown_grammar"


def test_scored_expected_match_penalizes_duplicate_table_fallback():
    debug_report = _load_debug_report()

    expected = (
        "| № | Код | Русский |\n"
        "| --- | --- | --- |\n"
        "| 01 | й-A1 | Привет |\n"
        "| РАЗДЕЛ A SECTION ALPHA | merged subsection |  |\n"
        "| 02 | й-B2 | Москва |\n"
    )
    actual = (
        "| № | Код | Русский |\n"
        "| --- | --- | --- |\n"
        "| 01 | й-A1 | Привет |\n"
        "| РАЗДЕЛ A SECTION ALPHA | merged subsection |  |\n"
        "| 02 | й-B2 | Москва |\n"
        "\n"
        "01 й-A1 Привет\n"
        "РАЗДЕЛ A SECTION ALPHA merged subsection\n"
        "02 й-B2 Москва\n"
    )

    score = debug_report.scored_expected_match(actual, expected)

    assert score.text_match_percent == "100.00"
    assert float(score.markdown_grammar_percent) < 90.0
    assert score.match_percent == "100.00"
    assert score.failure_kind == "markdown_grammar"


def test_scored_expected_match_allows_small_non_table_anchor_footer():
    debug_report = _load_debug_report()

    expected = (
        "| № | Код | Русский |\n"
        "| --- | --- | --- |\n"
        "| 01 | й-A1 | Привет |\n"
        "| 02 | й-B2 | Москва |\n"
    )
    actual = expected + "\nExpected tokens й-A1 Привет\n"

    score = debug_report.scored_expected_match(actual, expected)

    assert score.markdown_grammar_gate == "pass"


def test_scored_expected_match_rejects_table_reference_without_markdown_structure():
    debug_report = _load_debug_report()

    expected = (
        "SAMPLE hard OCR table 10 x 14 русский English 中文 123 й\n"
        "Image-only PDF merged subsection rows Markdown placeholder cells\n"
        "01 й-A1 Привет\n"
    )
    actual = (
        "| № | Код | Русский |\n"
        "| --- | --- | --- |\n"
        "| 01 | й-A1 | Привет |\n"
    )

    score = debug_report.scored_expected_match(actual, expected)

    assert score.markdown_grammar_percent == "0.00"
    assert score.markdown_grammar_notes == "reference_table_structure_missing"
    assert score.failure_kind == "markdown_grammar"


def test_scored_expected_match_separates_ocr_quality_loss_from_t9_candidate():
    debug_report = _load_debug_report()

    expected = "Привет мир Sample Alpha"
    lost_letters = "Првт мр Smpl"
    same_letters_wrong_words = "ПриветмирSampleAlpha"

    quality_loss = debug_report.scored_expected_match(lost_letters, expected)
    t9_candidate = debug_report.scored_expected_match(
        same_letters_wrong_words,
        expected,
    )

    assert quality_loss.compact_quality_gate == "fail"
    assert quality_loss.failure_kind == "ocr_quality_loss"
    assert t9_candidate.compact_quality_gate == "pass"


def test_compact_quality_penalizes_a_full_duplicate_symmetrically():
    debug_report = _load_debug_report()
    expected = "Привет мир Sample Alpha"
    actual = f"{expected}\n{expected}"

    score = debug_report.scored_expected_match(actual, expected)

    assert score.text_match_percent == "100.00"
    assert score.compact_quality_percent == "66.67"
    assert score.compact_quality_gate == "fail"
    assert score.failure_kind == "ocr_quality_loss"


def test_success_probability_uses_documented_weighted_layers():
    debug_report = _load_debug_report()

    assert debug_report.weighted_success_percent("87.00", "95.00", "90.00") == "87.84"


def test_sequence_token_score_uses_exact_lcs_for_repeated_table_symbols():
    debug_report = _load_debug_report()
    expected = tuple("|---|" * 2000)
    actual = tuple("|---|" * 1999 + "_")

    assert debug_report.sequence_token_score(actual, expected) == 99.95


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


def test_fuzzy_token_matches_compares_only_similar_length_tokens(monkeypatch):
    debug_report = _load_debug_report()

    def fail_substring_scan(*_args):
        raise AssertionError("token fuzzy matching must not scan long substrings")

    monkeypatch.setattr(debug_report, "fuzzy_compact_contains", fail_substring_scan)

    assert debug_report.fuzzy_token_matches(
        "математический",
        {"математическнй", "x" * 1000},
    )
    assert not debug_report.fuzzy_token_matches(
        "математический",
        {"математика" + "x" * 1000},
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

    actual = "УДОБНЫИ\n" "СПОСОБ\n" "ВЫУЧИТЬ\n" "НЕМЕЦКИИ ЯЗЫК\n" "BCЕ ПРАВИАА\n" "Москва\n" "Издатехьство ACT"
    expected = "УДОБНЫЙ СПОСОБ ВЫУЧИТЬ\n" "Lingua\n" "НЕМЕЦКИЙ ЯЗЫК\n" "ВСЕ ПРАВИЛА\n" "Москва\n" "Издательство АСТ"

    assert debug_report.expected_match(actual, expected) == ("83.33", "5", "6")


def test_expected_match_accepts_kazakh_ocr_confusables():
    debug_report = _load_debug_report()

    actual = "Баспа Аст ЖШК, 129085, Мэскеу к., Звёздный гулзар, " "21-уй, 1-курылыс, 705-белме, 7-кабат."
    expected = "«Баспа Аст» ЖШҚ, 129085, Мəскеу қ., Звёздный гүлзар, " "21-үй, 1-құрылыс, 705-бөлме, 7 қабат."

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
        "52.8.01¢H) Производственная практика исследовательская работа 7 5 " "36 180 180 5 12 Прикладная математика"
    )
    expected = (
        "| Б2.В.01(Н) | Производственная практика "
        "(научно-исследовательская работа) |  | 7 | 5 | 180 |  |  |  |  | "
        "180 |  |  |  |  |  |  | 5 |  | Прикладная математика |"
    )

    assert debug_report.expected_match(actual, expected) == ("100.00", "1", "1")


def test_expected_match_accepts_noisy_homework_labels():
    debug_report = _load_debug_report()

    actual = "HWZ(SCA) Jun 26\nНм (SCA) Jun 26"
    expected = "Hw7 (SCA) Jun 26\nHw7 (SCA) Jun 26"

    assert debug_report.expected_match(actual, expected) == ("100.00", "2", "2")
    assert debug_report.expected_match("Нм (SCA) Jun 26", "Hw7 (SCA) Jun 26") == (
        "100.00",
        "1",
        "1",
    )


def test_table_debug_artifact_uses_markdown_pipes(tmp_path):
    debug_report = _load_debug_report()
    result = tmp_path / "result.md"
    result.write_text(
        "# timing\n---\n| Name | Score |\n| --- | --- |\n| Alice | 10 |\n",
        encoding="utf-8",
    )
    tables = tmp_path / "tables"

    assert debug_report.write_table_markdown_files(result, tables) == 1
    assert (tables / "result.tables.md").read_text(encoding="utf-8") == ("| Name | Score |\n" "| Alice | 10 |\n")
    assert list(tables.glob("*.csv")) == []
    assert list(tables.glob("*.table-*.md")) == []


def test_markdown_grammar_compares_control_symbols_without_text():
    debug_report = _load_debug_report()
    actual = (
        "# noisy heading\n\n"
        "paragraph with unrelated OCR spelling\n\n"
        "- first noisy item\n"
        "- second noisy item\n"
        "- third noisy item"
    )
    expected = (
        "# clean heading\n\n"
        "clean paragraph\n\n"
        "- first item\n"
        "- second item\n"
        "- third item"
    )

    score = debug_report.scored_expected_match(actual, expected)
    assert score.markdown_grammar_percent == "100.00"
    assert score.markdown_grammar_gate == "pass"
    assert "lint=pass" in score.markdown_grammar_notes


def test_markdown_grammar_penalizes_extra_control_marker():
    debug_report = _load_debug_report()
    actual = "# heading\n\n- one\n- two\n- noise"
    expected = "# heading\n\n- one\n- two"

    score = debug_report.scored_expected_match(actual, expected)
    assert score.markdown_grammar_percent == "75.00"
    assert score.markdown_grammar_gate == "fail"


def test_markdown_grammar_ignores_non_structural_punctuation_noise():
    debug_report = _load_debug_report()
    expected = "# Заголовок:\n\nАбзац.\n\n- пункт."
    actual = "# _ Заголовок __:\n\nАбзац |.\n\n- |= пункт =."

    score = debug_report.scored_expected_match(actual, expected)

    assert score.markdown_grammar_percent == "100.00"
    assert "shadow=" in score.markdown_grammar_notes
    assert score.markdown_grammar_gate == "pass"


def test_markdown_grammar_ignores_line_breaks_and_letter_quality():
    debug_report = _load_debug_report()
    expected = "# Заголовок:\n\nАбзац, текст.\n\n- пункт."
    actual = "# Noisy:\nwrong, words.\n- item."

    score = debug_report.scored_expected_match(actual, expected)

    assert score.markdown_grammar_percent == "100.00"
    assert score.markdown_grammar_gate == "pass"


def test_markdown_grammar_scores_mixed_width_table_shapes_in_order():
    debug_report = _load_debug_report()
    expected = (
        "| A | B | C |\n"
        "| --- | --- | --- |\n"
        "| 1 | 2 | 3 |\n"
        "| 4 | 5 | 6 |\n\n"
        "| Key | Value |\n"
        "| --- | --- |\n"
        "| a | b |\n"
        "| c | d |\n"
        "| e | f |\n"
    )
    actual = (
        "| x | x | x |\n"
        "| --- | --- | --- |\n"
        "| x | x | x |\n"
        "| x | x | x |\n\n"
        "| x | x |\n"
        "| --- | --- |\n"
        "| x | x |\n"
        "| x | x |\n"
        "| x | x |\n"
        "| x | x |\n"
    )

    score = debug_report.table_shape_sequence_score(
        debug_report.markdown_table_rows(actual),
        debug_report.strict_reference_tables(expected),
    )

    assert score >= 90.0


def test_large_table_shape_filter_is_symmetric_for_tiny_reference_tables():
    debug_report = _load_debug_report()
    small = (
        "| Key | Value |\n"
        "| --- | --- |\n"
        "| a | b |\n"
        "| c | d |\n"
        "| e | f |\n"
    )
    large = (
        "| A | B | C | D |\n"
        "| --- | --- | --- | --- |\n"
        + "\n".join("| 1 | 2 | 3 | 4 |" for _ in range(40))
    )
    expected = f"{small}\n\n{large}"
    actual = f"{small}\n\n{large}"

    score = debug_report.table_shape_sequence_score(
        debug_report.markdown_table_rows(actual),
        debug_report.strict_reference_tables(expected),
    )

    assert score == 100.0


def test_markdown_linter_does_not_treat_data_as_broken_controls():
    debug_report = _load_debug_report()

    assert debug_report.lint_markdown_controls(
        "---\n-5\n#84} FA\nx+y\n",
    ) == ()
    assert debug_report.lint_markdown_controls("- \n####### bad") == (
        "line 1: invalid list item",
        "line 2: invalid heading",
    )


def test_markdown_shadow_uses_sparse_357_state_sums():
    debug_report = _load_debug_report()
    markdown = (
        "# Заголовок\n"
        "- пункт\n"
        "  - вложенный пункт\n"
        "| A | B |\n"
        "| --- | --- |\n"
        "| C | D |\n"
    )

    assert debug_report.markdown_shadow_codes(markdown) == (
        3,
        10,
        13,
        5,
        8,
    )
    assert debug_report.markdown_context_shadow_codes(markdown) == (
        10,
        13,
        7,
    )


def test_context_shadow_compresses_each_table_island_once():
    debug_report = _load_debug_report()
    markdown = (
        "| A | B |\n"
        "| --- | --- |\n"
        "| C | D |\n\n"
        "plain\n\n"
        "| E | F | G |\n"
        "| --- | --- | --- |\n"
        "| H | I | J |\n"
    )

    assert debug_report.markdown_context_shadow_codes(markdown) == (7, 7)
