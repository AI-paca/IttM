from app.formatting.structural_grammar import (
    _component_column_segments,
    SparseMarkdownRow,
    lint_markdown_structure,
    render_sparse_markdown_rows,
)
from app.formatting.structural_journal import (
    encode_structural_record,
    TemporaryStructuralJournal,
)


def test_sparse_merge_operations_render_heading_paragraph_and_list():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("Основные показатели",),
                anchor=(0, 0),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("статистики судов",),
                anchor=(1, 0),
                codes=((1, 0, 3),),
            ),
            SparseMarkdownRow(
                parts=("Обычный абзац",),
                anchor=(3, 0),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("продолжение абзаца",),
                anchor=(4, 0),
                codes=((4, 0, 3),),
            ),
            SparseMarkdownRow(
                parts=("- первый пункт",),
                anchor=(6, 1),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("продолжение", "окончание"),
                anchor=(7, 1),
                codes=((7, 1, 3),),
            ),
            SparseMarkdownRow(
                parts=("- второй пункт",),
                anchor=(9, 1),
                codes=(),
            ),
        ]
    )

    assert result.markdown == (
        "# Основные показатели статистики судов\n\n"
        "Обычный абзац продолжение абзаца\n\n"
        "- первый пункт продолжение окончание\n"
        "- второй пункт"
    )
    assert result.lint_errors == ()


def test_single_column_sparse_grid_stays_heading_and_paragraphs():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("Заголовок",),
                anchor=(0, 0),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("Обычный текст",),
                anchor=(2, 0),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("продолжение текста",),
                anchor=(3, 0),
                codes=((3, 0, 3),),
            ),
        ]
    )

    assert result.markdown == ("# Заголовок\n\n" "Обычный текст продолжение текста")
    assert "|" not in result.markdown
    assert result.lint_errors == ()


def test_zero_matrix_row_separates_neighboring_tables():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("header",),
                anchor=(0, 0),
                codes=((0, 1, 5),),
            ),
            SparseMarkdownRow(
                parts=("summary",),
                anchor=(1, 0),
                codes=((1, 0, 3),),
            ),
            SparseMarkdownRow(
                parts=("cards",),
                anchor=(2, 0),
                codes=tuple((2, column, 5) for column in range(2, 8)),
            ),
            SparseMarkdownRow(
                parts=("filters",),
                anchor=(4, 0),
                codes=((4, 2, 5),),
            ),
        ]
    )

    table_lines = [
        line for line in result.markdown.splitlines() if line.startswith("|") and not line.startswith("| ---")
    ]
    assert len(table_lines) == 3
    assert sum(line.startswith("| ---") for line in result.markdown.splitlines()) == 2
    assert "header summary" in table_lines[0]
    assert "cards" in table_lines[1]
    assert "filters" in table_lines[2]
    assert result.lint_errors == ()


def test_zero_matrix_column_separates_side_by_side_tables():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("left 1",),
                anchor=(0, 0),
                codes=((0, 1, 5),),
            ),
            SparseMarkdownRow(
                parts=("right 1",),
                anchor=(0, 3),
                codes=((0, 4, 5),),
            ),
            SparseMarkdownRow(
                parts=("left 2",),
                anchor=(1, 0),
                codes=((1, 0, 3), (1, 1, 5)),
            ),
            SparseMarkdownRow(
                parts=("right 2",),
                anchor=(1, 3),
                codes=((1, 3, 3), (1, 4, 5)),
            ),
        ]
    )

    assert result.markdown.count("| --- | --- |") == 2
    assert "| left 1 left 2 |  |" in result.markdown
    assert "| right 1 right 2 |  |" in result.markdown
    assert result.lint_errors == ()


def test_zero_row_before_column_zero_component_splits_heading_tail():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("Документ",),
                anchor=(0, 0),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("вводный текст",),
                anchor=(1, 0),
                codes=((1, 0, 3),),
            ),
            SparseMarkdownRow(
                parts=("Раздел",),
                anchor=(3, 0),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("первая строка",),
                anchor=(4, 0),
                codes=((4, 0, 3),),
            ),
            SparseMarkdownRow(
                parts=("вторая строка",),
                anchor=(5, 0),
                codes=((5, 0, 3),),
            ),
        ]
    )

    assert result.markdown == ("# Документ вводный текст\n\n" "## Раздел\n\n" "первая строка вторая строка")
    assert result.lint_errors == ()


def test_sparse_codes_are_additive_and_marker_drives_list_grammar():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("Заголовок",),
                anchor=(0, 0),
                codes=((0, 2, 5),),
            ),
            SparseMarkdownRow(
                parts=("продолжение",),
                anchor=(1, 0),
                codes=((1, 0, 3), (1, 2, 8)),
            ),
            SparseMarkdownRow(
                parts=("пункт",),
                anchor=(3, 1),
                codes=(),
                list_marker=True,
            ),
            SparseMarkdownRow(
                parts=("продолжение пункта",),
                anchor=(4, 1),
                codes=((4, 1, 3),),
            ),
        ]
    )

    assert result.markdown == ("| Заголовок продолжение |  |\n" "| --- | --- |\n\n" "- пункт продолжение пункта")
    assert result.lint_errors == ()


def test_single_late_merge_left_always_creates_table():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("Заголовок",),
                anchor=(0, 0),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("Логотип справа",),
                anchor=(2, 0),
                codes=((2, 3, 5),),
            ),
        ]
    )

    assert result.markdown == ("# Заголовок\n\n" "| Логотип справа |  |\n" "| --- | --- |")
    assert result.lint_errors == ()


def test_first_merge_left_component_renders_table():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("Заголовок слева", "Логотип справа"),
                anchor=(0, 0),
                codes=((0, 3, 5),),
            ),
        ]
    )

    assert result.markdown == ("| Заголовок слева Логотип справа |  |\n" "| --- | --- |")
    assert result.lint_errors == ()


def test_sparse_header_slots_do_not_expand_table_width_without_matrix_signal():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("Основные показатели",),
                anchor=(0, 0),
                codes=((0, 3, 5), (0, 4, 5), (0, 6, 5)),
            ),
            SparseMarkdownRow(
                parts=("НГУЭУ",),
                anchor=(1, 0),
                codes=((1, 0, 3), (1, 5, 5)),
            ),
        ]
    )

    assert result.markdown == ("| Основные показатели НГУЭУ |  |\n" "| --- | --- |")
    assert result.lint_errors == ()


def test_zero_rows_split_repeated_wide_table_rows():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("строка 1",),
                anchor=(0, 3),
                codes=((0, 4, 5), (0, 5, 5), (0, 6, 5)),
            ),
            SparseMarkdownRow(
                parts=("строка 2",),
                anchor=(2, 3),
                codes=((2, 4, 5), (2, 5, 5), (2, 6, 5)),
            ),
            SparseMarkdownRow(
                parts=("строка 3",),
                anchor=(4, 3),
                codes=((4, 4, 5), (4, 5, 5), (4, 6, 5)),
            ),
            SparseMarkdownRow(
                parts=("строка 4",),
                anchor=(6, 3),
                codes=((6, 4, 5), (6, 5, 5), (6, 6, 5)),
            ),
        ]
    )

    assert result.markdown.count("| --- | --- | --- | --- |") == 4
    for text in ("строка 1", "строка 2", "строка 3", "строка 4"):
        assert f"| {text} |  |  |  |" in result.markdown
    assert result.lint_errors == ()


def test_single_sparse_component_uses_merge_left_rows_as_table_rows():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("header",),
                anchor=(0, 0),
                codes=((0, 3, 5), (0, 7, 5), (0, 11, 5)),
            ),
            SparseMarkdownRow(
                parts=("continuation",),
                anchor=(1, 0),
                codes=((1, 0, 3),),
            ),
            SparseMarkdownRow(
                parts=("row 1",),
                anchor=(2, 0),
                codes=((2, 0, 3), (2, 3, 5), (2, 7, 5)),
            ),
            SparseMarkdownRow(
                parts=("noise",),
                anchor=(3, 0),
                codes=((3, 0, 3), (3, 7, 5)),
            ),
            SparseMarkdownRow(
                parts=("row 2",),
                anchor=(4, 0),
                codes=((4, 0, 3), (4, 3, 5), (4, 7, 5)),
            ),
        ]
    )

    assert result.markdown == (
        "| header |  |  |  |\n" "| --- | --- | --- | --- |\n" "| row 1 |  |  |  |\n" "| row 2 |  |  |  |"
    )
    assert result.lint_errors == ()


def test_sparse_matrix_signal_renders_full_component_table_rows():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("navigation",),
                anchor=(0, 0),
                codes=((0, 5, 5),),
            ),
            SparseMarkdownRow(
                parts=("filters",),
                anchor=(1, 0),
                codes=((1, 0, 3), (1, 5, 8)),
            ),
            SparseMarkdownRow(
                parts=("cards",),
                anchor=(2, 0),
                codes=tuple((2, column, 5) for column in range(2, 8)),
            ),
        ]
    )

    table_lines = [line for line in result.markdown.splitlines() if line.startswith("|")]
    assert len(table_lines) == 3
    assert table_lines[0] == "| navigation filters |  |  |  |  |  |  |"
    assert table_lines[1] == "| --- | --- | --- | --- | --- | --- | --- |"
    assert table_lines[2] == "| cards |  |  |  |  |  |  |"
    assert result.lint_errors == ()


def test_consecutive_up_only_columns_split_matrix_segments():
    rows = [
        SparseMarkdownRow(
            parts=("left",),
            anchor=(0, 0),
            codes=((0, 1, 5), (0, 2, 5), (0, 3, 5), (0, 4, 5), (0, 5, 5)),
        ),
        SparseMarkdownRow(
            parts=("left tail",),
            anchor=(1, 0),
            codes=((1, 1, 5), (1, 2, 3), (1, 3, 3)),
        ),
        SparseMarkdownRow(
            parts=("right",),
            anchor=(1, 4),
            codes=((1, 5, 5),),
        ),
        SparseMarkdownRow(
            parts=("left tail 2",),
            anchor=(2, 0),
            codes=((2, 1, 5), (2, 2, 3), (2, 3, 3)),
        ),
        SparseMarkdownRow(
            parts=("right 2",),
            anchor=(2, 4),
            codes=((2, 5, 5),),
        ),
    ]

    assert _component_column_segments(rows) == [(0, 1), (4, 5)]


def test_wide_merge_left_row_gaps_split_matrix_segments():
    rows = [
        SparseMarkdownRow(
            parts=("wide row",),
            anchor=(0, 0),
            codes=(
                *tuple((0, column, 5) for column in range(1, 5)),
                *tuple((0, column, 5) for column in range(7, 11)),
            ),
        ),
        SparseMarkdownRow(
            parts=("left tail",),
            anchor=(1, 0),
            codes=((1, 0, 3), (1, 3, 8)),
        ),
        SparseMarkdownRow(
            parts=("right tail",),
            anchor=(1, 7),
            codes=((1, 7, 3), (1, 10, 8)),
        ),
    ]

    assert _component_column_segments(rows) == [
        (0, 1, 2, 3, 4),
        (7, 8, 9, 10),
    ]


def test_wide_merge_left_gap_does_not_render_empty_matrix_segment():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("left wide row",),
                anchor=(0, 0),
                codes=(
                    *tuple((0, column, 5) for column in range(1, 5)),
                    *tuple((0, column, 5) for column in range(7, 11)),
                ),
            ),
            SparseMarkdownRow(
                parts=("left tail",),
                anchor=(1, 0),
                codes=((1, 0, 3), (1, 4, 8)),
            ),
        ]
    )

    assert sum(line.startswith("| ---") for line in result.markdown.splitlines()) == 1
    assert "left wide row" in result.markdown
    assert "left tail" in result.markdown
    assert result.lint_errors == ()


def test_repeating_vertical_grid_renders_filter_list_and_one_table():
    rows = [
        SparseMarkdownRow(
            parts=("filter one",),
            anchor=(0, 2),
            codes=((0, 12, 5),),
        ),
        SparseMarkdownRow(
            parts=("filter two",),
            anchor=(2, 2),
            codes=((2, 12, 5),),
        ),
        SparseMarkdownRow(
            parts=("filter three",),
            anchor=(4, 2),
            codes=((4, 12, 5),),
        ),
    ]
    for base in (8, 18, 28, 38):
        rows.extend(
            [
                SparseMarkdownRow(
                    parts=(f"card {base}",),
                    anchor=(base, 3),
                    codes=((base, 12, 5),),
                ),
                SparseMarkdownRow(
                    parts=(f"card {base} title",),
                    anchor=(base + 1, 3),
                    codes=((base + 1, 3, 3), (base + 1, 12, 8)),
                ),
                SparseMarkdownRow(
                    parts=(f"card {base} price",),
                    anchor=(base + 2, 3),
                    codes=((base + 2, 3, 3), (base + 2, 12, 8)),
                ),
                SparseMarkdownRow(
                    parts=(f"card {base} shop",),
                    anchor=(base + 3, 3),
                    codes=((base + 3, 3, 3), (base + 3, 12, 8)),
                ),
            ]
        )

    result = render_sparse_markdown_rows(rows)

    assert result.markdown.startswith("- filter one\n" "- filter two\n" "- filter three\n\n")
    assert sum(line.startswith("| ---") for line in result.markdown.splitlines()) == 1
    assert result.markdown.count("| card") == 4
    assert result.lint_errors == ()


def test_sparse_component_keeps_isolated_single_left_bridge_row():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("header",),
                anchor=(0, 0),
                codes=((0, 4, 5), (0, 8, 5), (0, 12, 5)),
            ),
            SparseMarkdownRow(
                parts=("fill",),
                anchor=(1, 0),
                codes=((1, 0, 3),),
            ),
            SparseMarkdownRow(
                parts=("fill",),
                anchor=(2, 0),
                codes=((2, 0, 3),),
            ),
            SparseMarkdownRow(
                parts=("fill",),
                anchor=(3, 0),
                codes=((3, 0, 3),),
            ),
            SparseMarkdownRow(
                parts=("row 1",),
                anchor=(4, 0),
                codes=((4, 0, 3), (4, 4, 5), (4, 8, 5)),
            ),
            SparseMarkdownRow(
                parts=("fill",),
                anchor=(5, 0),
                codes=((5, 0, 3),),
            ),
            SparseMarkdownRow(
                parts=("fill",),
                anchor=(6, 0),
                codes=((6, 0, 3),),
            ),
            SparseMarkdownRow(
                parts=("fill",),
                anchor=(7, 0),
                codes=((7, 0, 3),),
            ),
            SparseMarkdownRow(
                parts=("bridge",),
                anchor=(8, 0),
                codes=((8, 0, 3), (8, 8, 5)),
            ),
            SparseMarkdownRow(
                parts=("fill",),
                anchor=(9, 0),
                codes=((9, 0, 3),),
            ),
            SparseMarkdownRow(
                parts=("fill",),
                anchor=(10, 0),
                codes=((10, 0, 3),),
            ),
            SparseMarkdownRow(
                parts=("fill",),
                anchor=(11, 0),
                codes=((11, 0, 3),),
            ),
            SparseMarkdownRow(
                parts=("row 2",),
                anchor=(12, 0),
                codes=((12, 0, 3), (12, 4, 5), (12, 8, 5)),
            ),
        ]
    )

    assert result.markdown == (
        "| header |  |  |  |\n"
        "| --- | --- | --- | --- |\n"
        "| row 1 |  |  |  |\n"
        "| bridge |  |  |  |\n"
        "| row 2 |  |  |  |"
    )
    assert result.lint_errors == ()


def test_indented_tail_after_list_renders_section_list():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("Intro",),
                anchor=(0, 0),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("first",),
                anchor=(2, 1),
                codes=(),
                list_marker=True,
                content_left=60,
            ),
            SparseMarkdownRow(
                parts=("Generated assets",),
                anchor=(4, 0),
                codes=(),
                content_left=40,
            ),
            SparseMarkdownRow(
                parts=("When adding a case",),
                anchor=(5, 0),
                codes=((5, 0, 3),),
                content_left=40,
            ),
            SparseMarkdownRow(
                parts=("keep the oracle explicit",),
                anchor=(6, 0),
                codes=((6, 0, 3),),
                content_left=40,
            ),
            SparseMarkdownRow(
                parts=("before the checklist",),
                anchor=(7, 0),
                codes=((7, 0, 3),),
                content_left=40,
            ),
            SparseMarkdownRow(
                parts=("describe it",),
                anchor=(8, 0),
                codes=((8, 0, 3),),
                content_left=56,
            ),
            SparseMarkdownRow(
                parts=("with seed",),
                anchor=(9, 0),
                codes=((9, 0, 3),),
                content_left=80,
            ),
            SparseMarkdownRow(
                parts=("connect metrics",),
                anchor=(10, 0),
                codes=((10, 0, 3),),
                content_left=56,
            ),
        ]
    )

    assert result.markdown == (
        "# Intro\n\n"
        "- first\n\n"
        "## Generated assets\n\n"
        "When adding a case keep the oracle explicit before the checklist\n\n"
        "1. describe it with seed\n"
        "2. connect metrics"
    )
    assert result.lint_errors == ()


def test_structural_grammar_discards_ocr_markdown_noise():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("_ Заголовок __:",),
                anchor=(0, 0),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("Абзац | с формулой x=5.",),
                anchor=(2, 0),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("- |= пункт =",),
                anchor=(4, 1),
                codes=(),
            ),
        ]
    )

    assert result.markdown == ("# Заголовок:\n\n" "Абзац с формулой x=5.\n\n" "- пункт")
    assert result.lint_errors == ()


def test_structural_linter_rejects_unmerged_list_continuation():
    assert lint_markdown_structure("- пункт\nпродолжение") == ("line 2: unmerged list continuation",)


def test_structural_linter_accepts_horizontal_rules_and_data_prefixes():
    assert lint_markdown_structure("---\n-5\n#84} FA") == ()


def test_structural_journal_uses_numeric_refs_and_round_trips_utf8():
    with TemporaryStructuralJournal() as journal:
        reference = journal.append(
            ("первая строка", "second | row"),
        )

        assert reference.offset >= 0
        assert reference.length > 0
        assert tuple(journal.parts(reference)) == (
            "первая строка",
            "second | row",
        )


def test_structural_record_contains_matrix_and_unmodified_ocr_parts():
    record = encode_structural_record(
        kind="sparse",
        parts=("OCR строка",),
        anchor=(4, 2),
        codes=((4, 2, 3), (4, 3, 8)),
        list_marker=True,
        content_left=120,
        flags=(
            "ocr_region_psm:6",
            "ocr_region_mask:local_dark",
        ),
    )

    assert '"anchor":[4,2]' in record
    assert '"codes":[[4,2,3],[4,3,8]]' in record
    assert '"parts":["OCR строка"]' in record
    assert '"flags":["ocr_region_psm:6","ocr_region_mask:local_dark"]' in record


def test_empty_structural_cell_advances_without_rendering_text():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=(),
                anchor=(0, 0),
                codes=((0, 1, 5),),
            ),
            SparseMarkdownRow(
                parts=("Реальный текст",),
                anchor=(2, 0),
                codes=(),
            ),
        ]
    )

    assert "Реальный текст" in result.markdown
    assert "|  |" not in result.markdown


def test_merge_left_always_builds_table_with_empty_fake_cells():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("Текст слева и текст справа",),
                anchor=(0, 0),
                codes=((0, 2, 5),),
            ),
        ]
    )

    assert result.markdown.startswith(
        "| Текст слева и текст справа |  |",
    )


def test_horizontal_table_start_does_not_consume_preceding_list_row():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("Заголовок",),
                anchor=(0, 0),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("последний пункт",),
                anchor=(5, 0),
                codes=(),
                list_marker=True,
            ),
            SparseMarkdownRow(
                parts=("A B C D",),
                anchor=(6, 0),
                codes=((6, 0, 3), (6, 1, 5), (6, 2, 5), (6, 3, 5)),
            ),
            SparseMarkdownRow(
                parts=("1 2 3 4",),
                anchor=(7, 0),
                codes=((7, 0, 3), (7, 1, 8), (7, 2, 8), (7, 3, 8)),
            ),
        ]
    )

    assert "- последний пункт" in result.markdown
    assert "| A B C D |" in result.markdown


def test_followup_sparse_chunk_can_start_with_a_list():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("пункт после таблицы",),
                anchor=(4, 1),
                codes=(),
                list_marker=True,
            ),
        ],
        first_heading_level=2,
    )

    assert result.markdown == "- пункт после таблицы"


def test_anchor_one_to_anchor_three_starts_and_continues_list():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("Заголовок",),
                anchor=(0, 0),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("Абзац.",),
                anchor=(2, 1),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("первый пункт без распознанного тире",),
                anchor=(4, 3),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("второй пункт без распознанного тире",),
                anchor=(6, 3),
                codes=(),
            ),
        ]
    )

    assert "- первый пункт без распознанного тире" in result.markdown
    assert "- второй пункт без распознанного тире" in result.markdown


def test_non_adjacent_indent_does_not_invent_list_marker():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("Заголовок",),
                anchor=(0, 0),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("Абзац.",),
                anchor=(2, 0),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("Глубокий текст без столбца маркера",),
                anchor=(4, 2),
                codes=(),
            ),
        ]
    )

    assert "- Глубокий текст" not in result.markdown


def test_indented_component_without_markers_becomes_ordered_list_run():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("Принципы",),
                anchor=(0, 0),
                codes=(),
                content_left=40,
            ),
            SparseMarkdownRow(
                parts=("Один контракт.",),
                anchor=(2, 1),
                codes=(),
                content_left=56,
            ),
            SparseMarkdownRow(
                parts=("продолжение",),
                anchor=(3, 1),
                codes=((3, 1, 3),),
                content_left=80,
            ),
            SparseMarkdownRow(
                parts=("Один резолвер.",),
                anchor=(4, 1),
                codes=((4, 1, 3),),
                content_left=56,
            ),
            SparseMarkdownRow(
                parts=("хвост",),
                anchor=(5, 1),
                codes=((5, 1, 3),),
                content_left=80,
            ),
            SparseMarkdownRow(
                parts=("Один профиль.",),
                anchor=(6, 1),
                codes=((6, 1, 3),),
                content_left=56,
            ),
            SparseMarkdownRow(
                parts=("длинный хвост",),
                anchor=(7, 1),
                codes=((7, 1, 3),),
                content_left=80,
            ),
        ]
    )

    assert result.markdown == (
        "# Принципы\n\n" "1. Один контракт. продолжение\n" "2. Один резолвер. хвост\n" "3. Один профиль. длинный хвост"
    )
    assert result.lint_errors == ()


def test_empty_structural_row_does_not_break_anchor_list_transition():
    result = render_sparse_markdown_rows(
        [
            SparseMarkdownRow(
                parts=("Заголовок",),
                anchor=(0, 0),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("Абзац.",),
                anchor=(2, 1),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=(),
                anchor=(3, 2),
                codes=(),
            ),
            SparseMarkdownRow(
                parts=("пункт без распознанного тире",),
                anchor=(4, 3),
                codes=(),
            ),
        ]
    )

    assert "- пункт без распознанного тире" in result.markdown
