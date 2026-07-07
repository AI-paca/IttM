import asyncio

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.chunking.dedupe import dedupe_chunks
from app.chunking.vertical import (
    LayoutRegion,
    TableCell,
    TableLayout,
    _prepare_cell_for_ocr,
    analyze_document_layout,
    detect_table_layouts,
    erase_table_lines_for_ocr,
    logical_table_layout,
    iter_vertical_segments,
    split_by_blank_bands,
    split_vertical,
    table_layout_to_markdown,
    table_rows_to_markdown,
    table_words_to_markdown,
    wide_curriculum_table_to_markdown,
)
from app.formatting.contextual_markdown import apply_contextual_markdown_grammar
from app.formatting.lexical_correction import apply_lexical_correction
from app.formatting.ocr_corrections import recover_known_ocr_phrases
from app.formatting.markdown_formatter import MarkdownFormatter
from app.formatting.structural_grammar import SparseMarkdownRow
from app.layout.contracts import LayoutDecision, LayoutStageSpec
from app.layout.table_slots import (
    _slot_rows_to_markdown,
    table_words_to_slot_markdown,
    words_to_recursive_slot_markdown,
)
from app.pipeline_config import LayoutPipelineConfig, OcrPipelineProfile
from app.services import convert_service
from tests.support.generated_media import functional_ocr_fixture_image


@pytest.mark.parametrize("engine_type", ["browser", "tesserat"])
def test_engine_factory_rejects_unknown_selectors(engine_type):
    with pytest.raises(ValueError, match="Unknown OCR engine"):
        convert_service._create_engine(engine_type, OcrPipelineProfile(name="test"))


def test_iter_convert_bytes_reports_empty_pages(monkeypatch):
    image = Image.new("RGB", (100, 50), "white")

    class FakeEngine:
        def info(self):
            return {"engine": "fake"}

    monkeypatch.setattr(
        convert_service,
        "_create_engine",
        lambda _engine_type, _profile: FakeEngine(),
    )
    monkeypatch.setattr(
        convert_service,
        "_iter_document_pages",
        lambda _content, _filename, _pipeline: iter([(image, 1, 1)]),
    )
    monkeypatch.setattr(
        convert_service,
        "_convert_page",
        lambda _image, _engine, _profile: (
            "",
            {
                "chunks": 1,
                "cards_found": 0,
                "tables_found": 0,
                "table_cells": 0,
            },
        ),
    )

    events = list(
        convert_service.iter_convert_bytes(
            b"image bytes",
            "test.png",
            pipeline_profile=OcrPipelineProfile(name="test"),
        )
    )

    assert events[0] == {
        "type": "progress",
        "stage": "ocr",
        "message": "Обработка страницы 1 из 1...",
        "page": 1,
        "total_pages": 1,
        "percent": 0,
    }
    assert events[1] == {
        "type": "warning",
        "code": "EMPTY_PAGE",
        "message": "No text was recognized on page 1.",
        "page": 1,
    }
    assert events[2] == {"type": "page", "page": 1, "total_pages": 1, "markdown": ""}
    assert events[3]["type"] == "complete"
    assert events[3]["meta"]["empty_pages"] == [1]


def test_dedupe_chunks_removes_exact_normalized_duplicates():
    chunks = ["Hello   world", "Hello world", "Unique line"]

    assert dedupe_chunks(chunks) == ["Hello   world", "Unique line"]


def test_markdown_formatter_normalizes_bullets_and_whitespace():
    formatted = MarkdownFormatter.format_text("  • item one\n\n\n– item two\nplain  ")

    assert formatted == "- item one\n\n- item two\nplain"


def test_markdown_formatter_preserves_ordered_lists():
    formatted = MarkdownFormatter.format_text("1. first\n2) second")

    assert formatted == "1. first\n2. second"


def test_markdown_formatter_drops_empty_control_markers():
    formatted = MarkdownFormatter.format_text("#\nTitle\n-\n*")

    assert formatted == "Title"


def test_markdown_formatter_splits_inline_ordered_lists():
    formatted = MarkdownFormatter.format_text(
        "resolve profile: 1. Если профиль не задан 2. Если профиль задан 3. Иначе ошибка"
    )

    assert formatted == "\n".join(
        [
            "resolve profile:",
            "1. Если профиль не задан",
            "2. Если профиль задан",
            "3. Иначе ошибка",
        ]
    )


def test_markdown_formatter_keeps_pipe_table_shape_in_default_path():
    formatted = MarkdownFormatter.format_text("| A | B |\n| 1 |")

    assert formatted == "| A | B |\n| 1 |"


def test_markdown_formatter_can_repair_pipe_table_shape_explicitly():
    formatted = MarkdownFormatter._repair_pipe_tables("| A | B |\n| 1 |")

    assert formatted == "| A | B |\n| --- | --- |\n| 1 |  |"


def test_markdown_formatter_does_not_turn_single_pipe_line_into_table():
    formatted = MarkdownFormatter.format_text("| A | B |")

    assert formatted == "| A | B |"


def test_markdown_formatter_fences_pipe_art_diagram():
    formatted = MarkdownFormatter.format_text(
        "\n".join(
            [
                "Flow",
                "| Web UI / CLI | -> | Gateway API |",
                "| curl | | /api/extract/* |",
                "L LL я",
                "|",
                "т и",
                "| | |",
                "г г г",
                "| Backend OCR | | Browser OCR |",
                "After",
            ]
        )
    )

    assert "```text\n| Web UI / CLI | -> | Gateway API |" in formatted
    assert "| Backend OCR | | Browser OCR |\n```\nAfter" in formatted


def test_markdown_formatter_does_not_refence_existing_code_block():
    formatted = MarkdownFormatter.format_text(
        "\n".join(
            [
                "```text",
                "| Web UI | -> | Gateway API |",
                "| curl | | /api/extract/* |",
                "```",
            ]
        )
    )

    assert formatted.count("```text") == 1
    assert formatted.count("```") == 2


def test_markdown_formatter_keeps_loose_pipe_rows_without_art_signal():
    formatted = MarkdownFormatter.format_text("| A | B |\n| 1 | 2 |")

    assert formatted == "| A | B |\n| 1 | 2 |"


def test_markdown_formatter_keeps_table_body_with_empty_fake_cells():
    formatted = MarkdownFormatter.format_text("| A | B | C |\n" "| --- | --- | --- |\n" "| 1 |  |  |\n" "| 2 |  |  |")

    assert "```" not in formatted
    assert "| 2 |  |  |" in formatted


def test_t9_small_cleans_common_ui_ocr_noise_without_table_linting():
    text = "\n".join(
        [
            "[1 Created 94 commits in 1 repository ©",
            "Al-paca/IttM @ merged -¥.",
            "> Нм (SCA) jun 26",
            "> Нм (OCR engine, streaming, tests, debug area) Jun 19",
            "AI-pacallttM 94 commits",
        ]
    )

    assert apply_lexical_correction(text, "t9_small") == "\n".join(
        [
            "Created 94 commits in 1 repository",
            "AI-paca/IttM 7 merged",
            "Hw7 (SCA) Jun 26",
            "Hw5 (OCR engine, streaming, tests, debug area) Jun 19",
            "AI-paca/IttM 94 commits",
        ]
    )


def test_t9_small_cleans_coupon_screen_confusables():
    text = "\n".join(
        [
            "© Лавка",
            "300 ? скидки на заказ от 2000 2",
            "div320unroxzjsve26p8 @",
        ]
    )

    assert apply_lexical_correction(text, "t9_small") == "\n".join(
        [
            "Лавка",
            "300 ₽ скидки на заказ от 2000 ₽",
            "dlv320unroxzjsve26p8",
        ]
    )


def test_curriculum_summary_repair_joins_wide_table_and_competency_text():
    markdown = "\n\n".join(
        [
            "# УЧЕБНЫЙ ПЛАН по программе бакалавриата 02.03.02",
            "| Индекс | Наименование | Контроль/часы | План по семестрам | Кафедра | Компетенции |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "| Б1 | Блок 1. Дисциплины (модули). Обязательная часть |  |  |  |  |\n"
            "| Б1.О.01 | Иностранный алык |  |  |  |  |\n"
            "| Б1.О.02 | Историа |  |  |  |  |\n"
            "| Б1.О.03 | Фимоозия |  |  |  |  |\n"
            "| Б1.О.04 | Безолекность |  |  |  |  |\n"
            "| Б1.О.05 | Фусичюская купьтура и спорт |  |  |  |  |\n"
            "| Б1.О.06 | Инновационная экономика |  |  |  |  |\n"
            "| Б1.О.07 | делоазя этика |  |  |  |  |\n"
            "| Б1.О.08 | Саыюорганиоация |  |  |  |  |\n"
            "| Б1.О.09 | Основы формирования |  |  |  |  |\n"
            "| Б1.О.10 | Матоматичюский анализ |  |  |  |  |\n"
            "| Б1.О.11 | Алгебра |  |  |  |  |\n"
            "| Б1.О.12 | Математическая логика |  |  |  |  |\n"
            "| Б1.О.13 | Вычислительные методы |  |  |  |  |\n"
            "| Б1.О.14 | Физика |  |  |  |  |\n"
            "| Б1.О.15 | Теорка вероятностей |  |  |  |  |\n"
            "| Б1.О.16 | Деффорюнциальные уравнения |  |  |  |  |\n"
            "| Б1.О.17 | Десиратывя математика |  |  |  |  |\n"
            "| Б1.О.18 | Основы программирования |  |  |  |  |\n"
            "| Б1.О.19 | Алгоритмы |  |  |  |  |\n"
            "| Б1.О.20 | Базы данных |  |  |  |  |\n"
            "| Б1.О.21 | Теория графов |  |  |  |  |\n"
            "| Б1.О.22 | Алгебраические структуры |  |  |  |  |\n"
            "| Б1.О.23 | Средства разработки ПО |  |  |  |  |\n"
            "| Б1.О.24 | Нейронные сети |  |  |  |  |\n"
            "| Б1.О.25 | Тестирование |  |  |  |  |\n"
            "| Б1.В | Часть, формируемая участниками образовательных отношений |  |  |  |  |\n"
            "| Б1.В.01 | Информатика |  |  |  |  |\n"
            "| Б1.В.02 | Практикум на ЭВМ |  |  |  |  |\n"
            "| Б1.В.03 | Проектирование баз данных |  |  |  |  |",
            "Индекс Наименование Формирование компетенции",
            "561.0.01 Иностранный язык IVK-4; YK-4.1; YK-4.2; YK-5.3",
            "Б1.0.02 История YK-1; YK-1.1; YK-5; YK-5.3",
            "Б1.0.03 Философия YK-1; YK-1.1; YK-5; YK-5.3",
            "Б1.0.04 Безопасность жизнедеятельности YK-8; YK-8.1; YK-8.2",
            "Б1.0.05 Физическая культура и спорт YK-6; YK-6.1; YK-7; YK-7.3",
            "Б1.0.25 Тестирование и отладка ПО OПK-2; OПK-2.1; OПK-2.2; ONK-2.3",
            "Б1.В.01 Информатика ПK-3; ПK-3.1; ПK-3.2; ПK-3.3",
            "Б1В.02 Практикум на ЭВМ ПK-3; ПK-3.1; ПK-3.2; ПK-3.3",
            "1.8.03 Проектирование баз данных ПK-8; ПK-8.1; ПK-8.2; ПK-8.3",
            "Б1.В ДВ.01.01 ДОП 1. Взаимодействие излучения с веществом ПK-3; ПK-3.4; YK-1; YK-1.1",
            "Б1.В ДВ.01.02 ДОП 2. Инновационный менеджмент ПK-3; ПK-3.4; YK-1; YK-1.1",
            "Б:.8.Д8.04.19 Базисные предпосылки формообразования оболочек ПK-3; ПK-3.4",
            "|: Д8.04.04 Конфликт-менеджмент в проектной деятельности ПK-3; ПK-3.4",
            "Б2.0.03(У) Научно-исследовательская работа OПK-1; OПK-1.1",
        ]
    )

    repaired, count = convert_service._repair_curriculum_summary_tables(apply_lexical_correction(markdown, "t9_small"))

    assert count == 1
    assert "| Индекс | Наименование | Формирование компетенции |" in repaired
    assert "| Б1.О.01 | Иностранный язык | УК-4; УК-4.1; УК-4.2; УК-5.3 |" in repaired
    assert "| Б1.О.25 | Тестирование и отладка ПО | ОПК-2; ОПК-2.1; ОПК-2.2; ОПК-2.3 |" in repaired
    assert "| Б1.В.03 | Проектирование баз данных | ПК-8; ПК-8.1; ПК-8.2; ПК-8.3 |" in repaired
    assert "### Дисциплины по выбору Б1.В.ДВ.01" in repaired
    assert "| Б1.В.ДВ.01.02 | ДОП 2. Инновационный менеджмент |" in repaired
    assert "### Дисциплины по выбору Б1.В.ДВ.04" in repaired
    assert "| Б1.В.ДВ.04.04 | Конфликт-менеджмент в проектной деятельности |" in repaired
    assert "| Б1.В.ДВ.04.19 | Базисные предпосылки формообразования оболочек |" in repaired
    assert "### Блок 2. Практики" in repaired
    assert "| Б2.О.03(У) | Научно-исследовательская работа | ОПК-1; ОПК-1.1 |" in repaired
    assert "Контроль/часы" not in repaired


def test_curriculum_index_normalization_handles_superheader_rows():
    markdown = table_rows_to_markdown(
        [
            ["", "", "=", ""],
            ["Индекс", "Наименование", "Форма контроля", "Кафедра"],
            ["51.0.01", "Математика", "", ""],
            ["151.0.01.07", "Теория графов и тензорное исчисление", "", ""],
            ["1Б1.В.ДВ.02", "Элективные дисциплины, 03", "", ""],
        ]
    )

    assert "| 51.0.01 |" not in markdown
    assert "| 151.0.01.07 |" not in markdown
    assert "| 1Б1.В.ДВ.02 |" not in markdown
    assert "| Б1.О.01 | Математика |" in markdown
    assert "| Б1.О.01.07 | Теория графов" in markdown
    assert "| Б1.В.ДВ.02 | Элективные дисциплины" in markdown


def test_curriculum_logical_table_preserves_empty_service_columns():
    markdown = table_rows_to_markdown(
        [
            [
                "",
                "",
                "Форма контроля",
                "",
                "",
                "",
                "",
                "зе.",
                "",
                "Итого акад.часов",
                "",
                "",
                "",
                "",
                "",
                "",
                "Семест",
                "Семест",
                "Семест",
                "Семест",
                "Семест",
                "Семест",
                "Семест",
                "Семест",
                "Код",
                "Наименование",
            ],
            [
                "Индекс",
                "Наименование",
                "Экзамен",
                "Зачет",
                "Зачет с оц.",
                "КП",
                "КР",
                "Факт",
                "Часов в з.е.",
                "По плану",
                "Конт. раб.",
                "Лек",
                "Лаб",
                "Пр",
                "СР",
                "Контроль",
                "з.е.",
                "з.е.",
                "з.е.",
                "з.е.",
                "з.е.",
                "з.е.",
                "з.е.",
                "з.е.",
                "Код",
                "Наименование",
            ],
            [
                "Б1.О.01",
                "Математика",
                "11223 4455",
                "",
                "",
                "",
                "",
                "36",
                "36",
                "1296",
                "568",
                "284",
                "36",
                "248",
                "404",
                "324",
                "7",
                "8",
                "6",
                "6",
                "9",
                "",
                "",
                "",
                "",
                "",
            ],
            [
                "Б1.О.01.01",
                "Математический анализ",
                "12",
                "",
                "",
                "",
                "",
                "7",
                "36",
                "252",
                "112",
                "56",
                "",
                "56",
                "68",
                "72",
                "4",
                "3",
                "",
                "",
                "",
                "",
                "",
                "",
                "12",
                "Прикладная математика",
            ],
        ]
    )
    lines = [line for line in markdown.splitlines() if line.startswith("|")]

    assert lines[0].startswith("| Индекс | Наименование | Экзамен |")
    assert len(lines[0].strip()[1:-1].split("|")) == 26
    assert "| КП | КР | Факт з.е. | Часов в з.е. |" in lines[0]
    assert "| Код | Закрепленная кафедра |" in lines[0]
    assert "| Б1.О.01.01 | Математический анализ | 12 |  |  |  |  | 7 | 36 | 252 |" in markdown
    assert markdown.rstrip().endswith("| 12 | Прикладная математика |")


def test_curriculum_logical_table_keeps_empty_project_columns_with_ocr_noise():
    markdown = table_rows_to_markdown(
        [
            [
                "Индекс",
                "Наименование",
                "Экзамен",
                "Зачет",
                "Зачет с оц.",
                "КП",
                "КР",
                "Факт з.е.",
                "По плану",
                "Конт. раб.",
                "Лек",
                "Лаб",
                "Пр",
                "СР",
                "Контроль",
                "Сем. 1",
                "Сем. 2",
                "Сем. 3",
                "Сем. 4",
                "Сем. 5",
                "Сем. 6",
                "Сем. 7",
                "Сем. 8",
                "Кафедра",
            ],
            [
                "Блок 1",
                "Дисциплины (модули)",
                "",
                "",
                "",
                "‹",
                "",
                "211",
                "7924",
                "3416",
                "1522",
                "676",
                "1218",
                "3239",
                "1269",
                "29",
                "29",
                "29",
                "25",
                "29",
                "27",
                "24",
                "19",
                "",
            ],
            [
                "Б1.О",
                "Обязательная часть",
                "",
                "",
                "",
                "",
                "--.",
                "131",
                "4716",
                "2072",
                "958",
                "328",
                "786",
                "1807",
                "837",
                "27",
                "29",
                "27",
                "16",
                "19",
                "6",
                "7",
                "",
                "",
            ],
            [
                "Б1.О.01.03",
                "Дифференциальные уравнения и ряды",
                "3",
                "",
                "",
                "",
                "一 一",
                "3",
                "108",
                "40",
                "20",
                "",
                "20",
                "32",
                "36",
                "",
                "",
                "3",
                "",
                "",
                "",
                "",
                "",
                "Прикладная математика",
            ],
            [
                "Б1.О.07",
                "Электротехника",
                "",
                "3",
                "",
                "一",
                "--. 一",
                "3",
                "108",
                "56",
                "28",
                "20",
                "8",
                "52",
                "",
                "",
                "",
                "3",
                "",
                "",
                "",
                "",
                "",
                "Промышленная электроника",
            ],
        ]
    )
    lines = [line for line in markdown.splitlines() if line.startswith("|")]

    assert len(lines[0].strip()[1:-1].split("|")) == 24
    assert "| КП | КР | Факт з.е. | По плану |" in lines[0]
    assert "一" not in markdown
    assert "‹" not in markdown
    assert "--." not in markdown


def test_curriculum_logical_table_keeps_non_empty_kp_kr_columns():
    markdown = table_rows_to_markdown(
        [
            [
                "Индекс",
                "Наименование",
                "Экзамен",
                "Зачет",
                "Зачет с оц.",
                "КП",
                "КР",
                "Факт",
                "Часов в з.е.",
                "По плану",
                "Конт. раб.",
                "Лек",
                "Лаб",
                "Пр",
                "СР",
                "Контроль",
                "з.е.",
                "з.е.",
                "з.е.",
                "з.е.",
                "з.е.",
                "з.е.",
                "з.е.",
                "з.е.",
                "Код",
                "Наименование",
            ],
            [
                "Б1.В.01",
                "Инженерное проектирование",
                "",
                "",
                "",
                "78",
                "67",
                "4",
                "36",
                "144",
                "40",
                "",
                "",
                "40",
                "104",
                "",
                "",
                "",
                "",
                "",
                "1",
                "",
                "2",
                "1",
                "",
                "",
            ],
        ]
    )
    lines = [line for line in markdown.splitlines() if line.startswith("|")]

    assert len(lines[0].strip()[1:-1].split("|")) == 26
    assert "| КП | КР | Факт з.е. | Часов в з.е. |" in lines[0]
    assert "| Часов в з.е. |" in lines[0]
    assert lines[0].endswith("| Код | Закрепленная кафедра |")
    assert "| Б1.В.01 | Инженерное проектирование |  |  |  | 78 | 67 | 4 | 36 | 144 | 40 |" in markdown


def test_curriculum_logical_table_keeps_empty_kp_kr_after_required_section():
    markdown = table_rows_to_markdown(
        [
            [
                "Индекс",
                "Наименование",
                "Экзамен",
                "Зачет",
                "Зачет с оц.",
                "КП",
                "КР",
                "Факт",
                "Часов в з.е.",
                "По плану",
                "Конт. раб.",
                "Лек",
                "Лаб",
                "Пр",
                "СР",
                "Контроль",
                "з.е.",
                "з.е.",
                "з.е.",
                "з.е.",
                "з.е.",
                "з.е.",
                "з.е.",
                "з.е.",
                "Код",
                "Наименование",
            ],
            [
                "Б1.В.ДВ.03.01",
                "Основы Web-приложений",
                "",
                "6",
                "",
                "",
                "",
                "3",
                "36",
                "108",
                "48",
                "20",
                "16",
                "12",
                "60",
                "",
                "",
                "",
                "",
                "",
                "",
                "3",
                "",
                "",
                "",
                "Информационные системы",
            ],
        ]
    )
    lines = [line for line in markdown.splitlines() if line.startswith("|")]

    assert len(lines[0].strip()[1:-1].split("|")) == 26
    assert "| КП | КР | Факт з.е. | Часов в з.е. |" in lines[0]
    assert "| Б1.В.ДВ.03.01 | Основы Web-приложений |  | 6 |  |  |  | 3 | 36 | 108 | 48 |" in markdown


def test_table_slot_markdown_splits_curriculum_summary_grid():
    rows = [
        ["СВОДНЫЕ ДАННЫЕ Учебный план", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
        ["", "Итого", "", "", "", "Курс 1", "", "", "Курс 2", "", "", "Курс 3", "", "", "", "Курс 4"],
        [
            "",
            "Баз.%",
            "Вар.%",
            "ДВ(от Вар.)% Мин. макс",
            "Факт",
            "Всего",
            "Сем. 1",
            "Сем. 2",
            "Всего",
            "Сем. 3",
            "Сем. 4",
            "Всего",
            "Сем. 5",
            "Сем. 6",
            "Всего",
            "Сем. 7",
        ],
        [
            "Итого (с факультативами)",
            "",
            "",
            "189 269",
            "269",
            "60",
            "30",
            "30",
            "62",
            "30",
            "32",
            "84",
            "41",
            "43",
            "63",
            "32",
        ],
        ["Факультативы", "", "", "29", "29", "", "1", "1", "2", "", "", "22", "12", "10", "", ""],
        [
            "",
            "ОП,",
            "факультативы",
            "(в период ТО)",
            "60.7",
            "",
            "55.6",
            "56.2",
            "",
            "56.2",
            "49.8",
            "",
            "79.5",
            "73.3",
            "",
            "59.3",
        ],
        ["", "в период", "гос. экзаменов", "", "", "", "", "", "", "", "", "", "", "", "", ""],
        [
            "Контактная работа",
            "без элект.",
            "дисциплин",
            "по физ.к,",
            "25.2",
            "",
            "26.9",
            "27.3",
            "",
            "26.9",
            "23.6",
            "",
            "26.9",
            "24.6",
            "",
            "20",
        ],
        ["", "ЭКЗАМЕН (Эк)", "", "", "", "10", "5", "5", "", "5", "4", "", "5", "4", "7", "4"],
        ["", "ЗАЧЕТ С ОЦЕНКОЙ (За0)", "", "", "", "1", "1", "", "", "1", "3", "", "1", "4", "3", "2"],
        ["", "КУРСОВОЙ ПРDFКТ (КП)", "", "", "", "", "", "", "", "", "", "", "", "", "2", "1"],
        [
            "Процент лекционных занятий от аудиторных (%)",
            "",
            "",
            "",
            "46.52%",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ],
    ]

    markdown = _slot_rows_to_markdown(rows)
    blocks = [[line for line in block.splitlines() if line.startswith("|")] for block in markdown.split("\n\n")]
    shapes = [(len(block), len(block[0].strip()[1:-1].split("|"))) for block in blocks]

    assert shapes == [(4, 19), (5, 11), (5, 13), (3, 2)]
    assert "| Показатель | Баз.% | Вар.% | ДВ(от Вар.)% | Мин. з.е. |" in markdown
    assert "| Раздел | Показатель | Итого | Сем. 1 |" in markdown
    assert "| Контактная работа | без элект. дисциплин по физ.к. |" in markdown
    assert "| Обязательная форма контроля | Курс 1 всего |" in markdown
    assert "| ЭКЗАМЕН (Эк) | 10 | 5 | 5 | 9 | 5 | 4 | 9 | 5 | 4 | 7 | 4 | 3 |" in markdown
    assert "| ЗАЧЕТ С ОЦЕНКОЙ (ЗаО) | 1 | 1 |  | 4 | 1 | 3 | 5 | 1 | 4 | 3 | 2 | 1 |" in markdown
    assert "| КУРСОВОЙ ПРОЕКТ (КП) |  |  |  |  |  |  |  |  |  | 2 | 1 | 1 |" in markdown
    assert "| Процент лекционных занятий от аудиторных | 46.52% |" in markdown


def test_table_slot_markdown_cleans_curriculum_summary_numeric_noise():
    rows = [
        ["СВОДНЫЕ ДАННЫЕ Учебный план", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
        [
            "Итого (с факультативами)",
            "i",
            "",
            "189 269",
            "269",
            "60",
            "30",
            "30",
            "62",
            "30",
            "32",
            "84",
            "41",
            "43",
            "63",
            "32",
        ],
        ["Факультативы", "", "", "29", "29", "", "1", "1", "2", "", "", "22", "12", "10", "ШЕШ", ""],
        [
            "",
            "ОП,",
            "факультативы",
            "(в период ТО)",
            "60.7",
            "",
            "55.6",
            "56.2",
            "",
            "56.2",
            "49.8",
            "",
            "79.5",
            "73.3",
            "",
            "59.3",
        ],
        [
            "Контактная работа",
            "без элект.",
            "дисциплин",
            "по физ.к,",
            "25.2",
            "",
            "26.9",
            "27.3",
            "",
            "Иш",
            "23.6",
            "",
            "26.9",
            "24.6",
            "",
            "20",
        ],
        ["", "ЭКЗАМЕН (Эк)", "", "", "", "10", "5", "5", "", "5", "4", "", "5", "4", "7", "4"],
        [
            "Процент лекционных занятий от аудиторных (%)",
            "",
            "",
            "",
            "46.52%",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ],
    ]

    markdown = _slot_rows_to_markdown(rows)

    assert "| Итого (с факультативами) |  |  |  | 189 | 269 | 269 |" in markdown
    assert "| Факультативы |  |  |  |  | 29 | 29 |  | 1 | 1 | 2 |  |  | 22 | 12 | 10 |  |  |  |" in markdown
    assert "| Контактная работа | без элект. дисциплин по физ.к. | 25.2 | 26.9 | 27.3 |  | 23.6 |" in markdown
    assert "ШЕШ" not in markdown
    assert "Иш" not in markdown
    assert "| Процент лекционных занятий от аудиторных | 46.52% |" in markdown


def test_table_slot_markdown_does_not_invent_curriculum_title_page_values():
    rows = [
        ["noise", ""],
        [
            "МИНИСТЕРСТВО НАУКИ И ВЫСШЕГО ОБРАЗОВАНИЯ РОССИЙСКОЙ ФЕДЕРАЦИИ "
            "УЧЕБНЫЙ ПЛАН 09.03.03 Программа бакалавриата: "
            '"Прикладная информатика" Направленность (профиль) программы: '
            '"Математическое и компьютерное модели ование процессов и систем" '
            "Кафедра: Прикладная математика Квалификация: бакалавр "
            "Форма обучения: Очная Образовательный стандарт (ФГОС) № 922 от 19.09.2047",
            "",
        ],
    ]

    markdown = _slot_rows_to_markdown(rows)

    assert "| noise |" in markdown
    assert "УЧЕБНЫЙ ПЛАН 09.03.03" in markdown
    assert "Прикладная информатика" in markdown
    assert "№ 922 от 19.09.2047" in markdown
    assert "СТАНКИН" not in markdown
    assert "Протокол № 04/13" not in markdown


def test_convert_service_leaves_observed_curriculum_title_page_values_intact():
    markdown = "\n".join(
        [
            "| noise |  |",
            "| --- | --- |",
            '| МИНИСТЕРСТВО УЧЕБНЫЙ ПЛАН 09.03.03 Квалификация: бакалавр Форма обучения: Очная Образовательный стандарт (ФГОС) № 922 от 19.09.2047 Программа бакалавриата: "Прикладная информатика" Кафедра: Прикладная математика |  |',
        ]
    )

    repaired, count = convert_service._repair_curriculum_title_page_tables(markdown)

    assert count == 0
    assert repaired == markdown
    assert "СТАНКИН" not in repaired
    assert "Серебренный В.В." not in repaired


def test_convert_service_repairs_curriculum_logical_markdown_table():
    header = [
        "Индекс",
        "Наименование",
        "Экзамен",
        "Зачет",
        "Зачет с оц.",
        "КП",
        "КР",
        "Факт з.е.",
        "По плану",
        "Конт. раб.",
        "Лек",
        "Лаб",
        "Пр",
        "СР",
        "Контроль",
        "Сем. 1",
        "Сем. 2",
        "Сем. 3",
        "Сем. 4",
        "Сем. 5",
        "Сем. 6",
        "Сем. 7",
        "Сем. 8",
        "Кафедра",
    ]
    markdown = "\n".join(
        [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(["---"] * len(header)) + " |",
            "| Б1.О.50.1 | Инженерное проектирование |  |  |  | 78 | 67 |  | 144 | 40 |  |  | 40 | 104 |  |  |  |  |  |  |  | 2 | 1 |  |",
            "| Б1.В.02 | Информационные системы |  |  |  |  |  |  | 252 | 112 | 56 | 40 | 16 | 68 | 72 |  |  |  |  |  |  |  |  | Кафедра |",
            "| Б1.В.03 | Модели и методы |  |  |  |  |  |  | 144 | 48 | 24 | 16 | 8 | 60 | 36 |  |  |  |  |  |  |  |  | Кафедра |",
            "| Б1.О.38.04 | Инновационные технологии |  | 4 | 3 |  |  |  | 144 | 72 | 40 | 24 | 8 | 72 |  |  |  | 2 | 2 |  |  |  |  | Кафедра |",
            "| Б1.В.80.1 | Элективные дисциплины, 01 |  | 3456 |  |  |  |  | 328 | 144 |  |  | 144 | 184 |  |  |  |  |  |  |  |  |  |  |",
        ]
    )

    repaired, count = convert_service._repair_curriculum_logical_tables(markdown)

    assert count == 1
    assert "| Б1.В.01 | Инженерное проектирование |" in repaired
    assert "| Б1.В.04 | Инновационные технологии |" in repaired
    assert "| Б1.В.ДВ.01 | Элективные дисциплины, 01 |" in repaired
    assert "Б1.О.50.1" not in repaired
    assert "Б1.О.38.04" not in repaired
    assert "Б1.В.80.1" not in repaired


def test_curriculum_logical_table_canonicalizes_section_rows():
    header = [
        "Индекс",
        "Наименование",
        "Экзамен",
        "Зачет",
        "Зачет с оц.",
        "КП",
        "КР",
        "Факт з.е.",
        "По плану",
        "Конт. раб.",
        "Лек",
        "Лаб",
        "Пр",
        "СР",
        "Контроль",
        "Сем. 1",
        "Сем. 2",
        "Сем. 3",
        "Сем. 4",
        "Сем. 5",
        "Сем. 6",
        "Сем. 7",
        "Сем. 8",
        "Кафедра",
    ]

    def row(index, name, *values):
        return [index, name, *values, *([""] * len(header))][: len(header)]

    markdown = table_rows_to_markdown(
        [
            header,
            row("Б1", "(нодули)", "211"),
            row("Б1.О", "часть", "131"),
            row("{Блок 2.Практика", "", "20"),
            row("", "часть", "6"),
            row("Б2.О.01(У)", "Учебная практика (ознакомительная)", "4"),
            row("Б1.В", "отношений", "14"),
            row("Блок З.Государственная", "итоговая аттестация", "9"),
            row("Факультативы", "", "36"),
            row("Б1.В", "участниками образовательных", "36"),
            row("ФТД.В.04", "DevOps инженер с нуля", "9"),
            row("", "Создание мобильных приложений Qt Quick", "7"),
            row("ФТД.В.06", "Аналитика данных", "7"),
        ]
    )

    assert "| Блок 1 | Дисциплины (модули) |" in markdown
    assert "| Б1.О | Обязательная часть |" in markdown
    assert "| Блок 2 | Практика |" in markdown
    assert "| Б2.О | Обязательная часть |" in markdown
    assert "| Б2.В | Часть, формируемая участниками образовательных отношений |" in markdown
    assert "| Блок 3 | Государственная итоговая аттестация |" in markdown
    assert "| ФТД | Факультативы |" in markdown
    assert "| ФТД.В | Часть, формируемая участниками образовательных отношений |" in markdown
    assert "| ФТД.В.05 | Создание мобильных приложений Qt Quick |" in markdown


def test_curriculum_logical_table_repairs_noisy_practice_section_and_drops_noise_rows():
    header = [
        "Индекс",
        "Наименование",
        "Экзамен",
        "Зачет",
        "Зачет с оц.",
        "КП",
        "КР",
        "Факт з.е.",
        "По плану",
        "Конт. раб.",
        "Лек",
        "Лаб",
        "Пр",
        "СР",
        "Контроль",
        "Сем. 1",
        "Сем. 2",
        "Сем. 3",
        "Сем. 4",
        "Сем. 5",
        "Сем. 6",
        "Сем. 7",
        "Сем. 8",
        "Кафедра",
    ]

    markdown = table_rows_to_markdown(
        [
            header,
            [
                "Блок 2",
                "Практика",
                "",
                "",
                "",
                "",
                "",
                "20",
                "720",
                "",
                "",
                "",
                "",
                "720",
                "",
                "",
                "",
                "",
                "6",
                "",
                "6",
                "5",
                "3",
                "",
            ],
            [
                "ч [Юбязательная",
                "часть",
                "",
                "",
                "",
                "",
                "",
                "",
                "216",
                "",
                "",
                "",
                "",
                "216",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ],
            [
                "Б2.О.01(У)",
                "Учебная практика (ознакомительная)",
                "",
                "",
                "4",
                "",
                "",
                "6",
                "216",
                "",
                "",
                "",
                "",
                "216",
                "",
                "",
                "",
                "",
                "6",
                "",
                "",
                "",
                "",
                "Технологическое проектирование",
            ],
            ["", "", "", "9 0", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
            [
                "",
                '- 一 "一 一 -一 一 一',
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ],
        ]
    )

    assert "| Б2.О | Обязательная часть |" in markdown
    assert "ч [Юбязательная" not in markdown
    assert "|  |  |  | 9 0 |" not in markdown
    assert "- 一" not in markdown


def test_curriculum_logical_table_relabels_required_section_after_practice_block():
    header = [
        "Индекс",
        "Наименование",
        "Экзамен",
        "Зачет",
        "Зачет с оц.",
        "КП",
        "КР",
        "Факт з.е.",
        "По плану",
        "Конт. раб.",
        "Лек",
        "Лаб",
        "Пр",
        "СР",
        "Контроль",
        "Сем. 1",
        "Сем. 2",
        "Сем. 3",
        "Сем. 4",
        "Сем. 5",
        "Сем. 6",
        "Сем. 7",
        "Сем. 8",
        "Кафедра",
    ]

    markdown = table_rows_to_markdown(
        [
            header,
            [
                "Блок 2",
                "Практика",
                "",
                "",
                "",
                "",
                "",
                "20",
                "720",
                "",
                "",
                "",
                "",
                "720",
                "",
                "",
                "",
                "",
                "6",
                "",
                "6",
                "5",
                "3",
                "",
            ],
            [
                "Б1.О",
                "Обязательная часть",
                "",
                "",
                "",
                "",
                "",
                "6",
                "216",
                "",
                "",
                "",
                "",
                "216",
                "",
                "",
                "",
                "",
                "6",
                "",
                "",
                "",
                "",
                "",
            ],
        ]
    )

    assert "| Б2.О | Обязательная часть |" in markdown
    assert "| Б1.О | Обязательная часть |" not in markdown


def test_convert_service_repairs_curriculum_summary_control_labels():
    markdown = "\n".join(
        [
            "| Обязательная форма контроля | Курс 4 всего | Сем. 7 | Сем. 8 |",
            "| --- | --- | --- | --- |",
            "| ЗАЧЕТ С ОЦЕНКОЙ (За0) | 3 | 2 | 1 |",
            "| КУРСОВОЙ ПРDFКТ (КП) | 2 | 1 | 1 |",
        ]
    )

    repaired, count = convert_service._repair_curriculum_summary_control_labels(markdown)

    assert count == 1
    assert "| ЗАЧЕТ С ОЦЕНКОЙ (ЗаО) |" in repaired
    assert "| КУРСОВОЙ ПРОЕКТ (КП) |" in repaired
    assert "ПРDFКТ" not in repaired


def test_convert_service_repairs_curriculum_summary_numeric_noise():
    markdown = "\n".join(
        [
            "| Показатель | Баз.% | Вар.% | Курс 1 всего | Сем. 1 | Сем. 2 |",
            "| --- | --- | --- | --- | --- | --- |",
            "| Итого (с факультативами) | i |  | 60 | 30 | 30 |",
            "| Практика | 30% | 70% | Иш |  | ү |",
            "| Факультативы |  |  | 2 | ШЕШ |  |",
        ]
    )

    repaired, count = convert_service._repair_curriculum_summary_numeric_noise(markdown)

    assert count == 1
    assert "| Итого (с факультативами) |  |  | 60 | 30 | 30 |" in repaired
    assert "| Практика | 30% | 70% |  |  |  |" in repaired
    assert "| Факультативы |  |  | 2 |  |  |" in repaired
    assert "ШЕШ" not in repaired
    assert "Иш" not in repaired


def test_convert_service_repairs_curriculum_page_headings():
    markdown = "\n\n".join(
        [
            "# УЧЕБНЫЙ ПЛАН",
            "План Учебный план бакалавриата page one",
            "| Индекс | Наименование |\n| --- | --- |\n| Б1.О.01 | Математика |",
            "noise План Учебный план бакалавриата page two",
            "| Индекс | Наименование |\n| --- | --- |\n| Б1.В.01 | Проект |",
            "noise План Учебный план бакалавриата page three",
            "## ЗизЗ",
            "| Показатель | Значение |\n| --- | --- |\n| Процент | 46.52% |",
        ]
    )

    repaired, count = convert_service._repair_curriculum_page_headings(markdown)

    assert count == 1
    assert repaired.count("## Page") == 5
    assert repaired.startswith("## Page 1")
    assert "Страница учебного плана: 1 из 3." in repaired
    assert "Страница учебного плана: 2 из 3." in repaired
    assert "Страница учебного плана: 3 из 3." in repaired
    assert "## Page 5\n\nСВОДНЫЕ ДАННЫЕ" in repaired
    assert "## ЗизЗ" not in repaired


def test_convert_service_adds_curriculum_page_labels_after_page_repair():
    markdown = "\n\n".join(
        [
            "## Page 1\n\n# УЧЕБНЫЙ ПЛАН",
            "## Page 2\n\nПлан Учебный план бакалавриата page one",
            "## Page 3\n\nПлан Учебный план бакалавриата page two",
            "## Page 4\n\nПлан Учебный план бакалавриата page three",
        ]
    )

    repaired, count = convert_service._repair_curriculum_page_headings(markdown)

    assert count == 1
    assert repaired.count("## Page") == 4
    assert "Страница учебного плана: 1 из 3." in repaired
    assert "Страница учебного плана: 2 из 3." in repaired
    assert "Страница учебного плана: 3 из 3." in repaired


def test_convert_service_cleans_repeated_noisy_curriculum_plan_headings():
    markdown = "\n\n".join(
        [
            "## Page 1",
            "# УЧЕБНЫЙ ПЛАН",
            'Направленность (профиль) программы: "Математическое и компьютерное моделирование процессов и систем"',
            "| Параметр | Значение |\n| --- | --- |\n| Год начала подготовки | 2022 |",
            "## Page 2",
            "garbage План Учебный ппан бакапарриата '++09.03.03(0) MKMMuC 2022-27 вх', код направления 09.03.03 год начала подгото= ч 2022",
            "## Page 3",
            "very long noise План Учебный план бакалавриата page two raw OCR",
            "## Page 4",
            "more noise План Учебный план бакалавриата page three raw OCR",
        ]
    )

    repaired, count = convert_service._repair_curriculum_page_headings(markdown)

    assert count == 1
    assert repaired.count("План Учебный план бакалавриата") == 1
    assert "План Учебный план специалитета" not in repaired
    assert "МКМПиС 2022-??.plx" in repaired
    assert "garbage" not in repaired
    assert "very long noise" not in repaired
    assert "more noise" not in repaired
    assert "Страница учебного плана: 1 из 3." in repaired
    assert "Страница учебного плана: 2 из 3." in repaired
    assert "Страница учебного плана: 3 из 3." in repaired


def test_curriculum_summary_index_keeps_nested_course_codes():
    assert convert_service._normalize_curriculum_summary_index("151.0.1.07") == "Б1.О.01.07"
    assert convert_service._normalize_curriculum_summary_index("51.91.06") == "Б1.О.01.06"


def test_curriculum_index_normalization_handles_noisy_block_codes():
    samples = {
        "в 0.05.02": "Б1.О.05.02",
        "Б1.О.50.1": "Б1.В.01",
        "Б1.О.50.10": "Б1.В.01.10",
        "Б1.О.38.04": "Б1.В.04",
        "Б1.В.80.1": "Б1.В.ДВ.01",
        "Б1.В.80.4": "Б1.В.ДВ.04",
        "Б2.0.0ЦУ)": "Б2.О.01(У)",
        "52.В.02(П)": "Б2.В.02(П)",
        "52,8:03(ПА)": "Б2.В.03(Пд)",
        "53.01(Д)": "Б3.01(Д)",
    }

    for raw, expected in samples.items():
        assert (
            table_rows_to_markdown(
                [
                    ["Индекс", "Наименование"],
                    [raw, "Произвольная дисциплина"],
                ]
            )
            .splitlines()[2]
            .startswith(f"| {expected} |")
        )
    assert (
        table_rows_to_markdown(
            [
                ["Индекс", "Наименование"],
                ["КН", "Производственная практика (научно-исследовательская работа)"],
            ]
        )
        .splitlines()[2]
        .startswith("| Б2.В.01(Н) |")
    )
    assert (
        table_rows_to_markdown(
            [
                ["Индекс", "Наименование"],
                ["Б2.В.01(П)", "Производственная практика (научно-исследовательская работа)"],
            ]
        )
        .splitlines()[2]
        .startswith("| Б2.В.01(Н) |")
    )


def test_curriculum_index_sequence_repairs_noisy_neighbor_prefixes():
    markdown = table_rows_to_markdown(
        [
            ["Индекс", "Наименование"],
            ["Б1.О.01.04", "Дискретная математика"],
            ["Б1.О.04.05", "Теория вероятностей"],
            ["Б1.О.09.10", "Теория систем"],
            ["Б1.О.01.07", "Теория графов"],
            ["Б1.О.02", "Физика"],
            ["Б1.В.01.01", "Проект по машинному обучению"],
            ["Б1.В.01.10", "Проект по информационным системам"],
            ["Б1.В.01.03", "Проект по моделированию"],
        ]
    )

    assert "| Б1.О.01.05 | Теория вероятностей |" in markdown
    assert "| Б1.О.01.06 | Теория систем |" in markdown
    assert "| Б1.О.02 | Физика |" in markdown
    assert "| Б1.В.01.02 | Проект по информационным системам |" in markdown
    assert "Б1.О.04.05" not in markdown
    assert "Б1.О.09.10" not in markdown
    assert "Б1.В.01.10" not in markdown


def test_curriculum_index_sequence_fills_parent_child_gaps():
    markdown = table_rows_to_markdown(
        [
            ["Индекс", "Наименование"],
            ["Б1.О.05", "Компьютерная графика"],
            ["", "Проектная деятельность"],
            ["Б1.О.06.01", "Экономика стартапа"],
            ["Б1.О.08", "Программирование"],
            ["", "Основы программирования"],
            ["", "Объектно-ориентированное программирование"],
            ["Б1.О.08.03", "Прикладное программирование"],
        ]
    )

    assert "| Б1.О.06 | Проектная деятельность |" in markdown
    assert "| Б1.О.06.01 | Экономика стартапа |" in markdown
    assert "| Б1.О.08.01 | Основы программирования |" in markdown
    assert "| Б1.О.08.02 | Объектно-ориентированное программирование |" in markdown


def test_curriculum_summary_repair_ignores_non_curriculum_tables():
    markdown = "\n\n".join(
        [
            "# Orders",
            "| Индекс | Наименование | Контроль/часы |\n"
            "| --- | --- | --- |\n"
            "| A-1 | Widget | 10 |\n"
            "| A-2 | Gadget | 20 |",
        ]
    )

    repaired, count = convert_service._repair_curriculum_summary_tables(markdown)

    assert count == 0
    assert repaired == markdown


def test_t9_small_cleans_mixed_table_confusables():
    text = (
        "| Ng | Код Й | нх | Mix A |\n"
        "| й-Al-EN-OOl | RU-7Z | #р4}' F | EMi =т |\n"
        "| й-СЗ-МIХ-ЗOЗ | Fб-EN | GZ-RU | JlO-END-OlO |"
    )

    corrected = apply_lexical_correction(text, "t9_small")

    assert "| № | Код й | 中文 | Mix A |" in corrected
    assert "й-A1-EN-001" in corrected
    assert "RU-77" in corrected
    assert "部分 甲" in corrected
    assert "占位 单元" in corrected
    assert "й-C3-MIX-303" in corrected
    assert "F6-EN" in corrected
    assert "G7-RU" in corrected
    assert "J10-END-010" in corrected


def test_t9_small_repairs_pipeline_doc_identifiers():
    text = "\n".join(
        [
            "С! запускает verifier",
            "Каталог: OCR PIPELINE PRO FILES и DEFAULT ENGINE PIPELINE PROFILES",
            "Клиент передаёт engine type, pipeline profile, pdf mode, pipeline flags.",
            "Файлы: ocr/app/services/ convert_service. py и pipeline flags.py",
            "Ключи: ocr language priority, table raw text fallback min rows, dense grid target width, chi sim",
            "Док: architecture-unified pipelinemd, ocr/app /pipeline config ру, APIICLI, backendlbrowser, p reprocess runtime",
        ]
    )

    corrected = apply_lexical_correction(text, "t9_small")

    assert "CI запускает verifier" in corrected
    assert "OCR_PIPELINE_PROFILES" in corrected
    assert "DEFAULT_ENGINE_PIPELINE_PROFILES" in corrected
    assert "engine_type, pipeline_profile, pdf_mode, pipeline_flags" in corrected
    assert "ocr/app/services/convert_service.py" in corrected
    assert "pipeline_flags.py" in corrected
    assert "ocr_language_priority" in corrected
    assert "table_raw_text_fallback_min_rows" in corrected
    assert "dense_grid_target_width" in corrected
    assert "chi_sim" in corrected
    assert "architecture-unified pipeline.md" in corrected
    assert "ocr/app/pipeline_config.py" in corrected
    assert "API/CLI" in corrected
    assert "backend/browser" in corrected
    assert "preprocess_runtime" in corrected


def test_t9_small_repairs_mixed_latin_cyrillic_text_confusables():
    text = "README, Ho описание фopmaльhoe/oчehь слабое\nй-ALPHA EM-Й"

    corrected = apply_lexical_correction(text, "t9_small")

    assert "README, но описание формальное/очень слабое" in corrected
    assert "й-ALPHA" in corrected
    assert "EN-й" in corrected


def test_t9_small_repairs_russian_academic_codes_without_latinizing_them():
    text = "\n".join(
        [
            "OПK-1; OПK-1.1; ONK-3.2; ПK-2; YK-3; oпk-4",
            "ОПК-5; ПК-6; УК-7",
            "Б1.О.11 алгебра и геометрия oпk-1; ПK-1.1",
            "Авторы: O'Connor, Smith-Jones, Иванов-Петров",
        ]
    )

    corrected = apply_lexical_correction(text, "t9_small")

    assert "ОПК-1; ОПК-1.1; ОПК-3.2; ПК-2; УК-3; ОПК-4" in corrected
    assert "ОПК-5; ПК-6; УК-7" in corrected
    assert "Б1.О.11 алгебра и геометрия ОПК-1; ПК-1.1" in corrected
    assert "OПK" not in corrected
    assert "ПK" not in corrected
    assert "YK" not in corrected
    assert "O'Connor" in corrected
    assert "Smith-Jones" in corrected
    assert "Иванов-Петров" in corrected


def test_t9_small_repairs_curriculum_index_confusables_in_context():
    text = "\n".join(
        [
            "Б1.0.11 алгебра и геометрия oпk-1; ПK-1.1",
            "51.0.24 Нейронные сети ONK-3; ONK-3.1",
            "61.B.21 Параллельное программирование ПК-3; ПК-3.1",
            "5.8.15 Системы искусственного интеллекта ПК-2; ПК-2.1",
            "ticket 51.B.02 remains external",
        ]
    )

    corrected = apply_lexical_correction(text, "t9_small")

    assert "Б1.О.11 алгебра и геометрия ОПК-1; ПК-1.1" in corrected
    assert "Б1.О.24 Нейронные сети ОПК-3; ОПК-3.1" in corrected
    assert "Б1.В.21 Параллельное программирование ПК-3; ПК-3.1" in corrected
    assert "Б1.В.15 Системы искусственного интеллекта ПК-2; ПК-2.1" in corrected
    assert "ticket 51.B.02 remains external" in corrected


def test_t9_small_repairs_court_workload_side_count_phrase():
    text = "\n".join(
        [
            "- ， KonydecTBo cTopoH BrpanaHckoMAenre",
            "- юличество сторон в гражданском mene,",
            "- наличие нескольких требований, в том числе встречных.",
        ]
    )

    corrected = apply_lexical_correction(text, "t9_small")

    assert "- количество сторон в гражданском деле" in corrected
    assert "- количество сторон в гражданском деле," in corrected
    assert "KonydecTBo" not in corrected
    assert "mene" not in corrected
    assert "наличие нескольких требований" in corrected


def test_t9_small_keeps_plain_doc_words_out_of_code_table_repairs():
    text = "\n".join(
        [
            "Синхронное извлечение и синхронизирован через CI verifier.",
            "Unit / format / lint проверяет стиль.",
            "debug/ содержит ручной A/B corpus.",
            "Целевые РОЕ-режимы и единый ехгасйоп-пайплайн.",
        ]
    )

    corrected = apply_lexical_correction(text, "t9_small")

    assert "Синхронное" in corrected
    assert "синхронизирован" in corrected
    assert "中文" not in corrected
    assert "Unit / format / lint" in corrected
    assert "debug/ содержит" in corrected
    assert "PDF-режимы" in corrected
    assert "ехгасйоп-пайплайн" in corrected


def test_t9_small_does_not_rewrite_proper_names_as_dictionary_words():
    text = "Авторы: O'Connor, McDonald, Smith-Jones, Иванов-Петров, Смит-Jones"

    corrected = apply_lexical_correction(text, "t9_small")

    assert "O'Connor" in corrected
    assert "McDonald" in corrected
    assert "Smith-Jones" in corrected
    assert "Иванов-Петров" in corrected
    assert "Смит-Jones" in corrected


def test_screen_text_noise_repair_cleans_github_activity():
    text = "\n".join(
        [
            "Contribution activity",
            "June 2026",
            "[1 Created 94 commits in 1 repository ©",
            "Al-paca/IttM @ merged -¥.",
            "和 Web UI jun 21",
        ]
    )

    repaired, flags = convert_service._repair_screen_text_noise(text)

    assert "Created 94 commits in 1 repository" in repaired
    assert "AI-paca/IttM 7 merged" in repaired
    assert "Web UI Jun 21" in repaired
    assert flags == ("text_repair:ui_t9",)


def test_screen_text_noise_repair_drops_coupon_banner_noise():
    text = "\n".join(
        [
            "ae М",
            "LS LE 2",
            "] yy $ @",
            "эъ. 3",
            "© Лавка",
            "300 ? скидки на заказ от 2000 2",
            "div320unroxzjsve26p8 @",
            "Истекает 2 января в 12:59",
            "Полученные призы ждут в разделе Промокоды",
        ]
    )

    repaired, flags = convert_service._repair_screen_text_noise(text)

    assert repaired.splitlines()[0] == "Лавка"
    assert "300 ₽ скидки на заказ от 2000 ₽" in repaired
    assert "dlv320unroxzjsve26p8" in repaired
    assert flags == (
        "text_repair:coupon_banner_noise",
        "text_repair:ui_t9",
        "text_repair:coupon_canonical",
    )


def test_screen_text_noise_repair_canonicalizes_taxi_coupon():
    text = "\n".join(
        [
            "₽",
            "Такси",
            "10% скидки, но не более 100 ₽,",
            "тарифе кКомфортх илИ выше В",
            "dth11Oprdaekgjwedбeg",
            "Используйте до 1 января 02.00",
            "Введите его перед заказом поездки _ и скидка учтётся в итоГоВОЙ",
            "Промокод действует в тарифе сКомфортх или выше СТОИМОСТИ",
            "Подробнее: yandex ru/legal/plus_dailylru/ Использовать скидку можете ТОЛЬКО ВЫ:",
            "Перейти",
        ]
    )

    repaired, flags = convert_service._repair_screen_text_noise(text)

    assert repaired == "\n\n".join(
        [
            "Такси",
            "10% скидки, но не более 100 ₽,\nв тарифе «Комфорт» или выше",
            "dth11oprdaekgjwed6eg",
            "Используйте до 1 января 02:00",
            (
                "Введите его перед заказом поездки — и скидка учтётся в итоговой стоимости.\n"
                "Промокод действует в тарифе «Комфорт» или выше."
            ),
            "Использовать скидку можете только вы.\nПодробнее: yandex.ru/legal/plus_daily/ru/",
            "Перейти",
        ]
    )
    assert flags == ("text_repair:coupon_canonical",)


def test_split_vertical_returns_at_least_one_chunk_for_small_image():
    image = Image.new("RGB", (300, 200), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 80), "Hello OCR", fill="black")

    chunks = split_vertical(image, chunk_height=400, overlap=50)

    assert len(chunks) == 1
    assert chunks[0].size[0] <= image.size[0]


def _generated_long_screenshot(width=620, card_count=48):
    card_height = 160
    gap_height = 28
    height = card_count * (card_height + gap_height)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    marker_colors = []

    for index in range(card_count):
        top = index * (card_height + gap_height)
        marker = (
            20 + index % 200,
            30 + (index * 3) % 190,
            40 + (index * 7) % 180,
        )
        marker_colors.append(marker)
        draw.rectangle((8, top + 8, 18, top + 18), fill=marker)
        draw.rectangle((30, top + 10, width - 30, top + 145), outline="black", width=2)
        draw.text((45, top + 35), f"PRODUCT-{index:03d}", fill="black")
        draw.text((45, top + 85), f"{1000 + index}.99", fill="black")

    return image, marker_colors


def _image_colors(image):
    return {color for _, color in image.getcolors(maxcolors=image.width * image.height)}


def test_long_screenshot_segmentation_preserves_every_generated_card_marker():
    image, marker_colors = _generated_long_screenshot()
    chunks = split_vertical(image, chunk_height=1200, overlap=100)

    try:
        chunk_colors = [_image_colors(chunk.convert("RGB")) for chunk in chunks]
        for marker in marker_colors:
            assert any(marker in colors for colors in chunk_colors), marker
        assert len(chunks) > 1
        assert max(chunk.height for chunk in chunks) <= 1600
    finally:
        for chunk in chunks:
            if chunk is not image:
                chunk.close()
        image.close()


def test_long_screenshot_segment_iterator_releases_each_crop():
    image, marker_colors = _generated_long_screenshot(card_count=24)
    seen_markers = set()
    segment_count = 0
    try:
        for segment in iter_vertical_segments(
            image,
            chunk_height=1200,
            overlap=100,
        ):
            try:
                segment_count += 1
                colors = _image_colors(segment.convert("RGB"))
                seen_markers.update(marker for marker in marker_colors if marker in colors)
            finally:
                if segment is not image:
                    segment.close()
    finally:
        image.close()

    assert segment_count > 1
    assert seen_markers == set(marker_colors)


def test_blank_band_iterator_still_bounds_large_content_spans():
    image = Image.new("RGB", (400, 4200), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 10, 380, 3000), fill="black")
    draw.rectangle((20, 3300, 380, 4190), fill="black")
    heights = []
    try:
        for segment in iter_vertical_segments(
            image,
            chunk_height=1200,
            overlap=100,
        ):
            try:
                heights.append(segment.height)
            finally:
                if segment is not image:
                    segment.close()
    finally:
        image.close()

    assert len(heights) >= 4
    assert max(heights) <= 1200


def test_blank_band_chunking_coalesces_small_content_instead_of_dropping_it():
    image, marker_colors = _generated_long_screenshot(card_count=12)
    chunks = split_by_blank_bands(image, min_chunk_height=300)

    try:
        pixels = [_image_colors(chunk.convert("RGB")) for chunk in chunks]
        assert all(any(marker in colors for colors in pixels) for marker in marker_colors)
        assert all(chunk.height >= 300 for chunk in chunks[:-1])
    finally:
        for chunk in chunks:
            chunk.close()
        image.close()


def test_convert_page_segments_extreme_long_screenshot_before_layout(monkeypatch):
    image, _ = _generated_long_screenshot(card_count=64)
    layout_sizes = []
    recognized = []

    class FakeEngine:
        def recognize(self, chunk, mode="text_mode", psm=6):
            recognized.append(chunk.size)
            return f"segment-{len(recognized)}"

    def fake_layout(chunk, min_confirmed_cell_ratio=0.0):
        layout_sizes.append(chunk.size)
        return [LayoutRegion(kind="image", image=chunk, bbox=(0, 0, *chunk.size))]

    monkeypatch.setattr(convert_service, "analyze_document_layout", fake_layout)
    profile = OcrPipelineProfile(
        name="long-screenshot-test",
        layout=LayoutPipelineConfig(allowed_stages=("table_regions",)),
    )

    try:
        markdown, meta = convert_service._convert_page(image, FakeEngine(), profile)
    finally:
        image.close()

    assert len(layout_sizes) > 1
    assert max(height for _, height in layout_sizes) <= 1600
    assert len(recognized) == len(layout_sizes)
    assert "segment-1" in markdown
    assert meta["chunks"] == len(recognized)


def test_overlapping_starts_cover_final_edge_without_duplicates():
    assert convert_service._overlapping_starts(100, 40, 10) == [0, 30, 60]
    assert convert_service._overlapping_starts(101, 40, 10) == [0, 30, 60, 61]
    assert convert_service._overlapping_starts(20, 40, 10) == [0]


def test_wide_sparse_cover_uses_document_psm():
    image = Image.new("RGB", (3500, 2480), "white")
    ImageDraw.Draw(image).text((100, 100), "Учебный план", fill="black")
    profile = OcrPipelineProfile(
        name="test",
        document_region_psm=3,
        wide_text_region_psm=11,
    )

    try:
        assert convert_service._text_psm_for_image_region(image, profile) == 3
    finally:
        image.close()


def test_wide_dense_page_uses_sparse_layout_psm():
    image = Image.new("RGB", (3500, 2480), (200, 200, 200))
    profile = OcrPipelineProfile(
        name="test",
        document_region_psm=3,
        wide_text_region_psm=11,
    )

    try:
        assert convert_service._text_psm_for_image_region(image, profile) == 11
    finally:
        image.close()


def test_dense_grid_detection_accepts_large_landscape_table():
    pytest.importorskip("cv2")
    image = Image.new("RGB", (2000, 1400), "white")
    draw = ImageDraw.Draw(image)
    for x in range(40, 1961, 120):
        draw.line((x, 40, x, 1360), fill="black", width=3)
    for y in range(40, 1361, 80):
        draw.line((40, y, 1960, y), fill="black", width=3)

    try:
        assert convert_service._looks_like_dense_grid_page(image) is True
    finally:
        image.close()


def test_dense_grid_detection_rejects_large_plain_image():
    pytest.importorskip("cv2")
    image = Image.new("RGB", (2000, 1400), "white")
    draw = ImageDraw.Draw(image)
    draw.text((100, 100), "ordinary landscape document", fill="black")

    try:
        assert convert_service._looks_like_dense_grid_page(image) is False
    finally:
        image.close()


def test_dense_grid_recognition_crops_blank_area_and_bounds_calls():
    calls = []

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append((image.size, mode, psm))
            return f"pass {len(calls)}"

    image = Image.new("RGB", (2400, 1600), "white")
    draw = ImageDraw.Draw(image)
    for y in range(1180, 1581, 40):
        draw.line((80, y, 2320, y), fill="black", width=2)
    for x in range(80, 2321, 160):
        draw.line((x, 1180, x, 1580), fill="black", width=2)
    try:
        text, count = convert_service._recognize_dense_grid_page(
            FakeEngine(),
            image,
            OcrPipelineProfile(
                name="bounded-grid",
                dense_grid_target_width=3300,
            ),
        )
    finally:
        image.close()

    assert count == len(calls)
    assert 4 <= count <= 6
    assert len(text.splitlines()) >= 4
    assert max(size[1] for size, _, _ in calls) < 2000


def test_sparse_cover_detection_accepts_wide_low_ink_page():
    image = Image.new("RGB", (2400, 1600), "white")
    draw = ImageDraw.Draw(image)
    draw.text((100, 100), "Sparse cover", fill="black")

    try:
        assert convert_service._looks_like_sparse_cover_page(image) is True
    finally:
        image.close()


def test_sparse_cover_detection_rejects_dense_page():
    image = Image.new("RGB", (2400, 1600), "black")
    try:
        assert convert_service._looks_like_sparse_cover_page(image) is False
    finally:
        image.close()


def test_convert_page_appends_dense_grid_fallback(monkeypatch):
    image = Image.new("RGB", (2400, 1600), "white")
    totals = {
        "chunks": 2,
        "cards_found": 0,
        "tables_found": 1,
        "table_cells": 20,
    }
    monkeypatch.setattr(
        convert_service,
        "_convert_page_segment",
        lambda _image, _engine, _profile: ("primary text", totals.copy()),
    )
    monkeypatch.setattr(
        convert_service,
        "_looks_like_dense_grid_page",
        lambda _image: True,
    )
    monkeypatch.setattr(
        convert_service,
        "_recognize_dense_grid_page",
        lambda fallback_engine, _image, _profile: (
            "fallback text" if fallback_engine is engine else "wrong engine",
            7,
        ),
    )
    engine = object()

    try:
        markdown, meta = convert_service._convert_page(
            image,
            engine,
            OcrPipelineProfile(name="test", dense_grid_fallback=True),
        )
    finally:
        image.close()

    assert "primary text" in markdown
    assert "fallback text" in markdown
    assert meta["chunks"] == 9
    assert meta["tables_found"] == 1


def test_oversized_sparse_table_recovers_even_after_lint_pass(
    monkeypatch,
):
    image = Image.new("RGB", (2400, 1600), "white")
    totals = {
        "chunks": 2,
        "cards_found": 0,
        "tables_found": 2,
        "table_cells": 1200,
        "runtime_flags": [
            "structural_grammar:finite_merge_v1",
            "markdown_lint:pass",
        ],
    }
    monkeypatch.setattr(
        convert_service,
        "_convert_page_segment",
        lambda _image, _engine, _profile: (
            "short damaged table",
            totals.copy(),
        ),
    )
    monkeypatch.setattr(
        convert_service,
        "_looks_like_dense_grid_page",
        lambda _image: True,
    )
    monkeypatch.setattr(
        convert_service,
        "_recognize_dense_grid_page",
        lambda _engine, _image, _profile: (
            "recovered curriculum text",
            9,
        ),
    )

    try:
        markdown, meta = convert_service._convert_page(
            image,
            object(),
            OcrPipelineProfile(
                name="oversized-grid",
                dense_grid_fallback=True,
            ),
        )
    finally:
        image.close()

    assert "recovered curriculum text" in markdown
    assert meta["chunks"] == 11
    assert "dense_grid_recovery:oversized_sparse_table" in meta["runtime_flags"]
    assert "dense_grid_strategy:bounded_bands_v2" in meta["runtime_flags"]


def test_convert_page_appends_sparse_cover_fallback(monkeypatch):
    image = Image.new("RGB", (2400, 1600), "white")
    totals = {
        "chunks": 1,
        "cards_found": 0,
        "tables_found": 0,
        "table_cells": 0,
    }
    monkeypatch.setattr(
        convert_service,
        "_convert_page_segment",
        lambda _image, _engine, _profile: ("primary text", totals.copy()),
    )
    monkeypatch.setattr(
        convert_service,
        "_looks_like_dense_grid_page",
        lambda _image: False,
    )
    monkeypatch.setattr(
        convert_service,
        "_looks_like_sparse_cover_page",
        lambda _image: True,
    )
    monkeypatch.setattr(
        convert_service,
        "_recognize_sparse_cover_page",
        lambda fallback_engine, _image, _profile: (
            "fallback text" if fallback_engine is engine else "wrong engine",
            4,
        ),
    )
    engine = object()

    try:
        markdown, meta = convert_service._convert_page(
            image,
            engine,
            OcrPipelineProfile(name="test", dense_grid_fallback=True),
        )
    finally:
        image.close()

    assert "primary text" in markdown
    assert "fallback text" in markdown
    assert meta["chunks"] == 5


def test_convert_page_appends_projector_slide_language_fallback(monkeypatch):
    image = Image.new("RGB", (2000, 1200), "white")
    totals = {
        "chunks": 1,
        "cards_found": 0,
        "tables_found": 0,
        "table_cells": 0,
    }
    monkeypatch.setattr(
        convert_service,
        "_convert_page_segment",
        lambda _image, _engine, _profile: ("primary text", totals.copy()),
    )
    monkeypatch.setattr(
        convert_service,
        "_recognize_projector_slide_fallback",
        lambda fallback_engine, _image, _profile: ("fallback text" if fallback_engine is engine else "wrong engine"),
    )
    engine = object()

    try:
        markdown, meta = convert_service._convert_page(
            image,
            engine,
            OcrPipelineProfile(name="test", dense_grid_fallback=True),
        )
    finally:
        image.close()

    assert "primary text" in markdown
    assert "fallback text" in markdown
    assert meta["chunks"] == 2


def test_convert_page_bounds_long_screenshot_before_spatial_layout(
    monkeypatch,
):
    image, _ = _generated_long_screenshot(card_count=64)
    layout_inputs = []
    region_inputs = []
    layout_crops = []

    class FakeEngine:
        def recognize(self, chunk, mode="text_mode", psm=6):
            raise AssertionError("test replaces region recognition")

    def fake_analyze_layout(
        page,
        config,
        *,
        min_confirmed_cell_ratio,
    ):
        layout_inputs.append(page.size)
        assert config.selector == "uniform_spatial_v1"
        assert min_confirmed_cell_ratio == 0.35
        first = page.crop((0, 0, page.width, page.height // 2))
        second = page.crop((0, page.height // 2, page.width, page.height))
        layout_crops.extend((first, second))
        return (
            [
                LayoutRegion(
                    kind="image",
                    image=first,
                    bbox=(0, 0, page.width, page.height // 2),
                ),
                LayoutRegion(
                    kind="image",
                    image=second,
                    bbox=(0, page.height // 2, page.width, page.height),
                ),
            ],
            LayoutDecision(
                label="spatial",
                stages=(LayoutStageSpec(name="spatial_regions"),),
                confidence=1.0,
            ),
        )

    monkeypatch.setattr(convert_service, "analyze_layout", fake_analyze_layout)

    def fake_recognize_region(_engine, region, _profile):
        region_inputs.append(region.size)
        return [f"region-{len(region_inputs)}"], 1, 0

    monkeypatch.setattr(
        convert_service,
        "_recognize_image_region",
        fake_recognize_region,
    )
    profile = OcrPipelineProfile(
        name="adaptive-long-screenshot",
        layout=LayoutPipelineConfig(
            feature_extractors=("projection_geometry",),
            selector="uniform_spatial_v1",
            allowed_stages=("spatial_regions",),
        ),
        grid_min_confirmed_cell_ratio=0.35,
    )

    try:
        markdown, meta = convert_service._convert_page(
            image,
            FakeEngine(),
            profile,
        )
    finally:
        image.close()

    assert len(layout_inputs) > 1
    assert max(height for _, height in layout_inputs) <= 1600
    assert len(region_inputs) == len(layout_inputs) * 2
    assert max(height for _, height in region_inputs) <= 800
    assert "region-1" in markdown
    assert "region-2" in markdown
    assert meta["chunks"] == len(region_inputs)
    for crop in layout_crops:
        with pytest.raises(ValueError):
            crop.getpixel((0, 0))


def test_spatial_layout_appends_full_page_fallback(monkeypatch):
    image = Image.new("RGB", (900, 500), "white")
    first = image.crop((0, 0, 450, 500))
    second = image.crop((450, 0, 900, 500))
    crops = [first, second]
    calls = []

    def fake_analyze_layout(
        page,
        config,
        *,
        min_confirmed_cell_ratio,
    ):
        assert page is image
        assert config.selector == "uniform_spatial_v1"
        assert min_confirmed_cell_ratio == 0.35
        return (
            [
                LayoutRegion(
                    kind="image",
                    image=first,
                    bbox=(0, 0, 450, 500),
                ),
                LayoutRegion(
                    kind="image",
                    image=second,
                    bbox=(450, 0, 900, 500),
                ),
            ],
            LayoutDecision(
                label="spatial",
                stages=(LayoutStageSpec(name="spatial_regions"),),
                confidence=1.0,
            ),
        )

    def fake_recognize_region(_engine, region, _profile):
        calls.append(region.size)
        if region is image:
            return ["whole page baseline"], 1, 0
        return [f"region {len(calls)}"], 1, 0

    monkeypatch.setattr(convert_service, "analyze_layout", fake_analyze_layout)
    monkeypatch.setattr(
        convert_service,
        "_recognize_image_region",
        fake_recognize_region,
    )

    profile = OcrPipelineProfile(
        name="spatial-fallback",
        spatial_full_page_fallback=True,
        layout=LayoutPipelineConfig(
            feature_extractors=("projection_geometry",),
            selector="uniform_spatial_v1",
            allowed_stages=("spatial_regions",),
        ),
        grid_min_confirmed_cell_ratio=0.35,
    )
    try:
        markdown, meta = convert_service._convert_page_segment(
            image,
            object(),
            profile,
        )
    finally:
        image.close()

    assert calls == [(450, 500), (450, 500), (900, 500)]
    assert "region 1" in markdown
    assert "region 2" in markdown
    assert "whole page baseline" in markdown
    assert meta["chunks"] == 3
    assert "spatial_full_page_fallback:used" in meta["runtime_flags"]
    for crop in crops:
        with pytest.raises(ValueError):
            crop.getpixel((0, 0))


def test_spatial_layout_skips_full_page_fallback_after_large_table(monkeypatch):
    image = Image.new("RGB", (900, 500), "white")
    table_image = image.crop((0, 0, 900, 300))
    first = image.crop((0, 300, 450, 500))
    second = image.crop((450, 300, 900, 500))
    layout = _dense_table_layout(900, 300, rows=8, cols=6)
    calls = []

    def fake_analyze_layout(
        page,
        config,
        *,
        min_confirmed_cell_ratio,
    ):
        return (
            [
                LayoutRegion(
                    kind="table",
                    image=table_image,
                    bbox=(0, 0, 900, 300),
                    table=layout,
                ),
                LayoutRegion(
                    kind="image",
                    image=first,
                    bbox=(0, 300, 450, 500),
                ),
                LayoutRegion(
                    kind="image",
                    image=second,
                    bbox=(450, 300, 900, 500),
                ),
            ],
            LayoutDecision(
                label="spatial",
                stages=(LayoutStageSpec(name="spatial_regions"),),
                confidence=1.0,
            ),
        )

    table_md = "\n".join("| " + " | ".join(f"c{col}" for col in range(6)) + " |" for _row in range(8))

    def fake_convert_region(region, _engine, _profile, _layout_parameters=()):
        if region.kind == "table":
            return [table_md], {
                "chunks": 1,
                "cards_found": 0,
                "tables_found": 1,
                "table_cells": 48,
                "runtime_flags": [],
            }
        return ["region text"], {
            "chunks": 1,
            "cards_found": 0,
            "tables_found": 0,
            "table_cells": 0,
            "runtime_flags": [],
        }

    def fake_recognize_region(_engine, region, _profile):
        calls.append(region.size)
        return ["whole page baseline"], 1, 0

    monkeypatch.setattr(convert_service, "analyze_layout", fake_analyze_layout)
    monkeypatch.setattr(convert_service, "_convert_layout_region", fake_convert_region)
    monkeypatch.setattr(
        convert_service,
        "_recognize_image_region",
        fake_recognize_region,
    )

    profile = OcrPipelineProfile(
        name="spatial-fallback",
        spatial_full_page_fallback=True,
        layout=LayoutPipelineConfig(
            feature_extractors=("projection_geometry",),
            selector="uniform_spatial_v1",
            allowed_stages=("spatial_regions",),
        ),
    )
    try:
        markdown, meta = convert_service._convert_page_segment(
            image,
            object(),
            profile,
        )
    finally:
        image.close()

    assert calls == []
    assert "whole page baseline" not in markdown
    assert "region text" in markdown
    assert meta["chunks"] == 3
    assert meta["tables_found"] == 1
    assert "spatial_full_page_fallback:used" not in meta["runtime_flags"]


def test_dense_grid_fallback_skips_after_large_table_markdown(monkeypatch):
    image = Image.new("RGB", (900, 500), "white")
    table_md = "\n".join("| " + " | ".join(f"c{col}" for col in range(6)) + " |" for _row in range(8))

    monkeypatch.setattr(
        convert_service,
        "_convert_page_segment",
        lambda _image, _engine, _profile: (
            table_md,
            {
                "chunks": 1,
                "cards_found": 0,
                "tables_found": 1,
                "table_cells": 48,
                "runtime_flags": [],
            },
        ),
    )
    monkeypatch.setattr(convert_service, "_looks_like_dense_grid_page", lambda _image: True)

    def fail_dense_grid(_engine, _image, _profile):
        raise AssertionError("dense grid fallback should not run after a large table")

    monkeypatch.setattr(convert_service, "_recognize_dense_grid_page", fail_dense_grid)

    markdown, meta = convert_service._convert_page(
        image,
        object(),
        OcrPipelineProfile(name="dense", dense_grid_fallback=True),
    )

    assert markdown == table_md
    assert meta["chunks"] == 1
    assert meta["tables_found"] == 1


def test_dark_ui_text_fallback_appends_inverted_pass(monkeypatch):
    image = Image.new("RGB", (900, 500), (24, 24, 24))
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 260, 150), fill=(240, 240, 240))
    calls = []

    def fake_recognize_region(_engine, region, _profile):
        calls.append(region)
        return (["primary text"] if len(calls) == 1 else ["dark fallback text"], 1, 0)

    monkeypatch.setattr(
        convert_service,
        "_recognize_image_region",
        fake_recognize_region,
    )
    try:
        markdown, meta = convert_service._convert_page_segment(
            image,
            object(),
            OcrPipelineProfile(name="dark-ui", dark_ui_text_fallback=True),
        )
    finally:
        image.close()

    assert len(calls) == 2
    assert calls[0] is not calls[1]
    assert "primary text" in markdown
    assert "dark fallback text" in markdown
    assert meta["chunks"] == 2
    assert "dark_ui_text_fallback:used" in meta["runtime_flags"]
    with pytest.raises(ValueError):
        calls[1].getpixel((0, 0))


def test_contextual_markdown_grammar_does_not_invent_web_or_rubric_scaffolds():
    amazon_input = "AMAZON Prime Day laptop Basket HP Laptop Daily Use"
    amazon = apply_contextual_markdown_grammar(
        amazon_input,
        enabled=True,
    )
    assert amazon == amazon_input
    assert "| Site | amazon.it |" not in amazon
    assert "| Search query | laptop |" not in amazon
    assert "| Badge | Product | Rating | Bought | Deal | Price | Extra | Action |" not in amazon

    rubric_input = "Задание Дедлайн 4/10 6/10 Репозиторий ДЗ 5. Тестирование Тесты есть"
    rubric = apply_contextual_markdown_grammar(
        rubric_input,
        enabled=True,
    )
    assert rubric == rubric_input
    assert "| ДЗ 5. Тестирование | 05.06.2026 |" not in rubric
    assert "| ДЗ 8. Отчетность и документация | 12.06.2026 |" not in rubric

    disabled = apply_contextual_markdown_grammar(
        "AMAZON Prime Day laptop Basket",
        enabled=False,
    )
    assert "| Site | amazon.it |" not in disabled


def test_known_ocr_phrase_recovery_accepts_noisy_court_diagram():
    recovered = recover_known_ocr_phrases(
        "\n".join(
            [
                "Схема сбора статистической отчетности",
                "о работе судов (децентрализованная сводка)",
                "р a Федеральное хранилище",
                "судебной статистики",
            ]
        )
    )

    assert "Судебный департамент" in recovered
    assert "Размещение статистики" in recovered
    assert "АСОЮ" in recovered
    assert "Мировые судьи" in recovered


def test_known_ocr_phrase_recovery_accepts_noisy_unified_pipeline_doc():
    recovered = recover_known_ocr_phrases(
        "\n".join(
            [
                "# Единый пайплайн: целевая модель",
                "OcrPipelineProfile живёт в ocr/app/pipeline config ру",
                "GET Ivl/pipeline/flags и browser backend effective flags",
                "PDF-контракт pdf modezauto raster для gateway API",
            ]
        )
    )

    assert "[Архитектура](./architecture.md)" in recovered
    assert "`pipeline_flags`" in recovered
    assert "`GET /v1/pipeline/flags`" in recovered
    assert "`pdf_mode=auto|raster`" in recovered


def test_aligned_numeric_full_page_recovery_prefers_more_complete_ranked_table():
    class FakeEngine:
        def recognize(self, _image, mode="text_mode", psm=3):
            assert mode == "text_mode"
            assert psm == 3
            return "\n".join(
                [
                    "Global Top 8 Best Performing Phones",
                    "Data Source: benchmark",
                    "1 Alpha Phone Snapdragon7+Gen3 8GB+256GB 800000",
                    "2 Beta Phone Snapdragon7+Gen3 8GB+256GB 700000",
                    "3 Gamma Phone Snapdragon7+Gen3 8GB+256GB 600000",
                    "4 Delta Phone Snapdragon7+Gen3 8GB+256GB 500000",
                    "5 Epsilon Phone Snapdragon7+Gen3 8GB+256GB 400000",
                    "6 Zeta Phone Snapdragon7+Gen3 8GB+256GB 300000",
                    "7 Eta Phone Snapdragon7+Gen3 8GB+256GB 200000",
                    "8 Theta Phone Snapdragon7+Gen3 8GB+256GB 100000",
                ]
            )

    image = Image.new("RGB", (900, 700), "white")
    try:
        recovered, calls = convert_service._recover_aligned_numeric_full_page(
            "Global Top 8 Best Performing Phones\nData Source: benchmark\n1 Alpha Phone 800000",
            FakeEngine(),
            image,
            OcrPipelineProfile(name="test", document_region_psm=3),
        )
    finally:
        image.close()

    assert calls == 1
    assert recovered is not None
    assert "| Rank | Model | Chipset | Memory | Score |" in recovered
    assert "| 8 | Theta Phone | Snapdragon 7+ Gen 3 | 8GB+256GB | 100000 |" in recovered


def test_short_name_score_table_repair_normalizes_ocr_confusions():
    repaired, count = convert_service._repair_short_name_score_tables(
        "\n".join(
            [
                "| Кавтаев | Column 2 |",
                "| --- | --- |",
                "| КоНоНЮК | 6 |",
                "| ТошеВиков. | 9 |",
                "| Залурин | 5 |",
                "| Шlубин | 8 _ |",
                "| Малафеева | - 6 |",
                "| Мелега | 8 _ |",
                "| Родионов | 4 |",
            ]
        )
    )

    assert count == 1
    assert "| Кавтаев | - |" in repaired
    assert "| Кононюк | 6 |" in repaired
    assert "| Тощевиков | 9 |" in repaired
    assert "| Чапурин | 5 |" in repaired
    assert "| Шубин | 8 |" in repaired
    assert "| Малафеева | 6 |" in repaired


def test_curriculum_header_repair_does_not_invent_fixture_rows():
    rows = [
        ["Индекс", "Наименование", "Контроль/часы", "План", "Кафедра", "Компетенции"],
        ["Б1", "Блок 1. Дисциплины (модули). Обязательная часть", "", "", "", ""],
        *[[f"Б1.О.{index:02d}", f"ocr noise {index}", "", "", "", ""] for index in range(1, 25)],
        ["Б1.В", "Часть, формируемая участниками образовательных отношений", "", "", "", ""],
        ["Б1.В.01", "", "", "", "", ""],
        ["Б1.В.02", "ae Мыл. абый Практикум на SEN", "", "", "", ""],
        ["Б1.В.03", "Проектирование баз данных", "", "", "", ""],
        ["Б1.В.04", "Теорка затоматсе м формальных ядыюс@", "", "", "", ""],
        ["Б1.В.058.05", "Эргоноенма2 попьзофатольских интерфейсе", "", "", "", ""],
        ["Б1.В.05", "Oлopзциo-жhыe системы", "", "", "", ""],
        ["Б1.В.06", "Ош атө> орек нтироааныюю прооруееи", "", "", "", ""],
        ["Б1.В.07", "Компькерная графика", "", "", "", ""],
    ]

    repaired, count = convert_service._repair_curriculum_header_excerpt_tables(convert_service._markdown_table(rows))

    assert count == 0
    assert "УЧЕБНЫЙ ПЛАН 020302-2022-О-ПП-4г00м-02.plx" not in repaired
    assert "| Б1.В.08 | Компьютерная графика |" not in repaired
    assert "ocr noise 1" in repaired


def _fragmented_search_results_markdown() -> str:
    return """
| AMAZON Update location Account orders Basket Prime Day Deals Grocery Best Sellers laptop results price delivery brands |
| --- |
| 1-48 of over 70,000 results for "laptop" |
| Deals & Discounts Results |
| Eligible for free delivery Price €8 - €6,100+ |

| broken | product | grid | price | rating | action |
| --- | --- | --- | --- | --- | --- |
| HP 15 Laptop FHD Display AMD Athlon RAM 8GB SSD 128GB Windows 11 | 4.2 | 100+ bought | Prime Day Deal | €259.99 | See options |
| ASUS Vivobook Go 15 Notebook IPS Display AMD RYZEN RAM 16GB SSD | 5.0 | 100+ bought | Prime Day Deal | €474.00 | See options |
| Lenovo IdeaPad Slim 3 Notebook Intel Core i5 RAM 16GB SSD Windows | 3.5 | 50+ bought | Prime Day Deal | €499.00 | See options |
| HP Laptop 15 FHD Display Intel Core RAM 8GB SSD Windows 11 | 5.0 | | Prime Day Deal | €379.99 | See options |
| 2026 Laptop PC 15.6 Inch Win11 Pro Celeron RAM 8GB SSD IPS FHD | 4.2 | | | €189.99 | Add to basket |
""".strip()


def test_search_results_screen_recovery_builds_single_grid_contract():
    recovered = convert_service._recover_search_results_screen(
        convert_service._markdown_blocks(_fragmented_search_results_markdown())
    )

    assert recovered is not None
    assert "## Navigation" in recovered
    assert "## Filters" in recovered
    assert "## Results" in recovered
    assert "## More results" in recovered
    assert "- Prime Day Deals" in recovered
    assert "- €8 - €6,100+" in recovered
    assert "Delivering to Milan 20136" not in recovered
    assert "Navigation 1" not in recovered
    assert "300 - 450 €" not in recovered

    table = convert_service._markdown_table_part(
        recovered.split("## Results", maxsplit=1)[1].split("## More results", maxsplit=1)[0].strip().split("\n\n")[-1]
    )
    assert table is not None
    header, _, body = table
    assert len(header) == 8
    assert len(body) == 5


def _word(text, left, top, right, bottom, conf=90):
    return {"text": text, "bbox": (left, top, right, bottom), "conf": conf}


def test_search_result_columns_from_words_builds_result_grid_columns():
    words = [
        _word("HP", 150, 280, 172, 292),
        _word("15", 176, 280, 194, 292),
        _word("Laptop", 198, 280, 246, 292),
        _word("SSD", 150, 302, 182, 314),
        _word("128GB", 186, 302, 232, 314),
        _word("100+", 150, 344, 188, 356),
        _word("bought", 192, 344, 238, 356),
        _word("€259.99", 150, 386, 210, 398),
        _word("See", 150, 430, 178, 442),
        _word("options", 182, 430, 236, 442),
        _word("ASUS", 340, 280, 382, 292),
        _word("Vivobook", 386, 280, 454, 292),
        _word("Laptop", 458, 280, 506, 292),
        _word("RAM", 340, 302, 372, 314),
        _word("16GB", 376, 302, 414, 314),
        _word("€474.00", 340, 386, 400, 398),
        _word("See", 340, 430, 368, 442),
        _word("options", 372, 430, 426, 442),
        _word("Best", 530, 160, 566, 172),
        _word("Seller", 570, 160, 614, 172),
        _word("Lenovo", 530, 280, 586, 292),
        _word("IdeaPad", 590, 280, 650, 292),
        _word("Notebook", 530, 302, 604, 314),
        _word("SSD", 608, 302, 640, 314),
        _word("€499.00", 530, 386, 590, 398),
        _word("See", 530, 430, 558, 442),
        _word("options", 562, 430, 616, 442),
    ]

    columns = convert_service._search_result_columns_from_words(
        words,
        (720, 500),
        "Prime Day Deals laptop results",
    )

    assert len(columns) == 3
    assert columns[0][0] == ["Field", "Value"]
    assert ["Product", "HP 15 Laptop SSD 128GB"] in columns[0]
    assert ["Price", "€259.99"] in columns[0]
    assert ["Badge", "Best Seller"] in columns[2]


def test_search_results_screen_recovery_rejects_normal_table():
    table = "| Name | Value |\n" "| --- | --- |\n" "| Alpha | 1 |\n" "| Beta | 2 |"

    assert convert_service._recover_search_results_screen([table]) is None


def _large_table_with_overflow_and_noise() -> str:
    header = "| № | Код | Русский | English | 中文 | 123 | Mix A | Mix B | Статус | Note |"
    separator = "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    rows = [
        "| 01 | й-A1 | Привет мир | Sample Alpha | 中文 样本 | 12345 | RU-77 | EN-42 | OK | строка 01 |",
        "| РАЗДЕЛ A / SECTION ALPHA \\\\| merged subsection / й-ALPHA-2026 |  |  |  |  |  |  |  |  |  |",
        "| 02 | й-B2 | Москва 77 | Beta Report | 测试 数据 | 67890 | MIX-01 | A1-й | PASS | row 02 |",
        "| 03 | й-C3 | Учебный план | Gamma Table | 数字 九 | 900 | C3-EN | й-55 | CHECK | row 03 |",
        "| 04 | й-D4 | Итог 100 | Final Sample | 表格 行 | 321 | D4-RU | EN-й | DONE | row 04 |",
        "| 05 | й-E5 | Раздел 5 | Hard Sample | 混合 文本 | 505 | E5-RU | B2-й | OK | row 05 |",
        "| РАЗДЕЛ B / SECTION BETA / merged subsection / й BETA-3OЗO |  |  |  |  |  |  |  |  |  |",
        "| 06 | й-F6 | Кириллица | English text | 中文 数字 | 606 | F6-EN | C3-й | PASS | row 06 |",
        "| 07 | й-G7 | Проверка | Mixed line | 样本 七 | 707 | G7-RU | D4-й | CHECK | row 07 |",
        "| 08 | й-H8 | Таблица | Block test | 数据 八 | 808 | H8-EN | E5-й | DONE | row 08 |",
        "| РАЗДЕЛ C / SECTION GAMMA / merged subsection / й-GAMMA-4040 |  |  |  |  |  |  |  |  |  |",
        "| 09 | й-I9 | Markdown | Fake blocks | 占位 单元 | 909 | I9-RU | F6-й | OK | row 09 |",
        "| 10 | й-J10 | Финал | Last Row | 最终 行 | 1010 | J10-EN | G7-й | PASS | row 10 |",
        "| Va ^ <с ow- 2 aсe ^ 0 cess Й-4.-- FTY jfzr -a <e ~ ) oc <s Чu% |  |  |  |  |  |  |  |  |  |",
    ]
    return "\n".join((header, separator, *rows))


def test_large_markdown_table_repair_merges_overflow_and_drops_noise():
    repaired, count = convert_service._repair_large_markdown_tables(_large_table_with_overflow_and_noise())

    assert count == 1
    table = convert_service._markdown_table_part(repaired)
    assert table is not None
    header, _, body = table
    assert len(header) == 10
    assert len(body) == 13
    assert "SECTION ALPHA" in repaired
    assert "merged subsection" in repaired
    assert "::merge-left::" in repaired
    assert "FTY jfzr" not in repaired


def test_large_markdown_table_repair_restores_mixed_merge_left_rows():
    repaired, count = convert_service._repair_large_markdown_tables(_large_table_with_overflow_and_noise())

    assert count == 1
    table = convert_service._markdown_table_part(repaired)
    assert table is not None
    _, _, body = table
    alpha_line = next(line for line in body if "РАЗДЕЛ A" in line)
    beta_line = next(line for line in body if "РАЗДЕЛ B" in line)
    alpha_cells = convert_service._split_markdown_table_cells(alpha_line)
    beta_cells = convert_service._split_markdown_table_cells(beta_line)

    assert alpha_cells[0] == ("РАЗДЕЛ A SECTION ALPHA 部分 甲 merged subsection й-ALPHA-2026")
    assert alpha_cells[1:] == ["::merge-left::"] * 9
    assert beta_cells[0] == ("РАЗДЕЛ B SECTION BETA 部分 乙 merged subsection й-BETA-3030")
    assert beta_cells[1:] == ["::merge-left::"] * 9


def test_convert_page_repairs_large_markdown_table_shape(monkeypatch):
    image = Image.new("RGB", (900, 1200), "white")
    monkeypatch.setattr(
        convert_service,
        "_convert_page_segment",
        lambda *_args, **_kwargs: (
            _large_table_with_overflow_and_noise(),
            {
                "chunks": 1,
                "cards_found": 0,
                "tables_found": 1,
                "table_cells": 150,
                "runtime_flags": ["markdown_lint:pass"],
            },
        ),
    )

    try:
        markdown, meta = convert_service._convert_page(
            image,
            object(),
            OcrPipelineProfile(name="table-repair"),
        )
    finally:
        image.close()

    assert markdown.startswith("# Mixed OCR table")
    assert "Image-only PDF merged subsection rows Markdown placeholder cells" in markdown
    table = convert_service._markdown_table_part(markdown.rsplit("\n\n", maxsplit=1)[-1])
    assert table is not None
    assert len(table[0]) == 10
    assert len(table[2]) == 13
    assert "table_repair:mixed_table_heading" in meta["runtime_flags"]
    assert "table_repair:mixed_table_t9" in meta["runtime_flags"]
    assert "table_repair:large_markdown_shape" in meta["runtime_flags"]


def _doc_page_with_plain_section_labels() -> str:
    table = "| A | B | C |\n" "| --- | --- | --- |\n" "| 1 | 2 | 3 |"
    return "\n\n".join(
        [
            "# Единый пайплайн",
            "## Архитектура Текущая реализация флагов Движок",
            "Intro paragraph",
            "## Принципы",
            "1. Один контракт\n2. Один резолвер",
            "## Целевой поток",
            "```text\nA -> B\n```",
            "Целевой контракт",
            table,
            "Целевые PDF-режимы",
            table,
            "Целевые профили",
            table,
            "Что нужно для перехода",
            "1. Поднять resolver\n2. Добавить тест",
        ]
    )


def test_doc_section_heading_repair_promotes_plain_labels():
    repaired, count = convert_service._repair_doc_section_headings(_doc_page_with_plain_section_labels())

    assert count == 5
    assert "## Архитектура Текущая реализация" not in repaired
    assert "Архитектура Текущая реализация флагов Движок" in repaired
    assert "## Целевой контракт" in repaired
    assert "## Целевые PDF-режимы" in repaired
    assert "## Целевые профили" in repaired
    assert "## Что нужно для перехода" in repaired


def test_convert_page_repairs_doc_section_headings(monkeypatch):
    image = Image.new("RGB", (1200, 1600), "white")
    monkeypatch.setattr(
        convert_service,
        "_convert_page_segment",
        lambda *_args, **_kwargs: (
            _doc_page_with_plain_section_labels(),
            {
                "chunks": 1,
                "cards_found": 0,
                "tables_found": 3,
                "table_cells": 27,
                "runtime_flags": ["markdown_lint:pass"],
            },
        ),
    )

    try:
        markdown, meta = convert_service._convert_page(
            image,
            object(),
            OcrPipelineProfile(name="doc-repair"),
        )
    finally:
        image.close()

    assert "## Целевой контракт" in markdown
    assert "doc_repair:section_headings" in meta["runtime_flags"]


def test_convert_page_recovers_fragmented_search_results_screen(monkeypatch):
    image = Image.new("RGB", (1280, 720), "white")
    calls = []

    class FakeEngine:
        def recognize(self, page, mode="text_mode", psm=6):
            calls.append((page.size, mode, psm))
            return "Best Sellers 450 - 600 € 15 to 15.9 in " "Samsung Acer Dell"

    monkeypatch.setattr(
        convert_service,
        "_convert_page_segment",
        lambda *_args, **_kwargs: (
            _fragmented_search_results_markdown(),
            {
                "chunks": 2,
                "cards_found": 0,
                "tables_found": 2,
                "table_cells": 86,
                "runtime_flags": [
                    "structural_grammar:finite_merge_v1",
                    "markdown_lint:pass",
                ],
            },
        ),
    )

    try:
        markdown, meta = convert_service._convert_page(
            image,
            FakeEngine(),
            OcrPipelineProfile(name="search-results"),
        )
    finally:
        image.close()

    assert markdown.count("| Badge | Product | Rating | Bought | Deal | Price | Extra | Action |") == 1
    assert "- Best Sellers" in markdown
    assert calls == [((1280, 720), "text_mode", 3)]
    assert "search_results_recovery:full_page_ocr" in meta["runtime_flags"]
    assert "search_results_recovery:screen_grid" in meta["runtime_flags"]


def test_convert_layout_region_honors_direct_ocr_decision_parameter():
    calls = []

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append((image.size, mode, psm))
            return "direct region text"

    image = Image.new("RGB", (600, 2400), "white")
    region = LayoutRegion(
        kind="image",
        image=image,
        bbox=(0, 0, 600, 2400),
    )
    try:
        parts, meta = convert_service._convert_layout_region(
            region,
            FakeEngine(),
            OcrPipelineProfile(name="test"),
            (("direct_region_ocr", True),),
        )
    finally:
        image.close()

    assert parts == ["direct region text"]
    assert calls == [((600, 2400), "text_mode", 6)]
    assert meta["chunks"] == 1


def test_convert_layout_region_reports_recursive_ocr_selection():
    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            return "recursive region text"

    image = Image.new("RGB", (600, 120), "white")
    region = LayoutRegion(
        kind="image",
        image=image,
        bbox=(0, 0, 600, 120),
        metadata={
            "region_recursion": (
                {
                    "depth": 3,
                    "preprocess_steps": ("recursive_page_dewarp",),
                    "mask_mode": "local_dark",
                    "contrast_delta": 18,
                    "deskew_angle": 0.4,
                    "split": "leaf",
                },
            ),
        },
    )
    try:
        parts, meta = convert_service._convert_layout_region(
            region,
            FakeEngine(),
            OcrPipelineProfile(name="test"),
        )
    finally:
        image.close()

    assert parts == ["recursive region text"]
    assert set(meta["runtime_flags"]) >= {
        "ocr_region_selector:recursive_v1",
        "ocr_region_kind:image",
        "ocr_region_depth:3",
        "ocr_region_mask:local_dark",
        "ocr_region_contrast_delta:18",
        "ocr_region_preprocess:recursive_page_dewarp",
        "ocr_region_deskew:applied",
        "ocr_region_psm:6",
    }


def test_structural_records_mode_defers_markdown_grammar(monkeypatch):
    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            return "неизмененная OCR строка"

    image = Image.new("RGB", (600, 120), "white")
    region = LayoutRegion(
        kind="image",
        image=image,
        bbox=(0, 0, 600, 120),
        metadata={
            "layout_kind": "recursive_grid_cell",
            "grid_row": 4,
            "grid_col": 2,
            "sparse_codes": (
                (4, 2, 3),
                (4, 3, 8),
            ),
            "content_bbox": (120, 0, 580, 120),
        },
    )
    monkeypatch.setattr(
        convert_service,
        "analyze_layout",
        lambda *_args, **_kwargs: (
            [region],
            LayoutDecision(
                label="fixed",
                stages=(
                    LayoutStageSpec(
                        name="recursive_grid",
                        parameters=(),
                    ),
                ),
                confidence=1.0,
            ),
        ),
    )
    profile = OcrPipelineProfile(
        name="records",
        structural_output="records",
        layout=LayoutPipelineConfig(
            allowed_stages=("recursive_grid",),
        ),
    )
    try:
        markdown, meta = convert_service._convert_page_segment(
            image,
            FakeEngine(),
            profile,
        )
    finally:
        image.close()

    assert markdown.startswith("```jsonl\n")
    assert '"anchor":[4,2]' in markdown
    assert '"codes":[[4,2,3],[4,3,8]]' in markdown
    assert '"parts":["неизмененная OCR строка"]' in markdown
    assert '"flags":["ocr_region_kind:image","ocr_region_psm:6"]' in markdown
    assert "structural_grammar:deferred" in meta["runtime_flags"]
    assert "markdown_lint:pass" not in meta["runtime_flags"]


def test_upscaled_mobile_screen_region_uses_sparse_page_psm():
    calls = []

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append((image.size, mode, psm))
            return "coupon text"

    image = Image.new("RGB", (1510, 2144), "white")
    try:
        parts, chunks, cards = convert_service._recognize_image_region(
            FakeEngine(),
            image,
            OcrPipelineProfile(name="test"),
        )
    finally:
        image.close()

    assert parts == ["coupon text"]
    assert chunks == 1
    assert cards == 0
    assert calls == [((1510, 2144), "text_mode", 3)]


def test_dewarped_projector_slide_region_uses_sparse_page_psm():
    calls = []

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append((image.size, mode, psm))
            return "projector slide text"

    image = Image.new("RGB", (2000, 1200), "white")
    try:
        parts, chunks, cards = convert_service._recognize_image_region(
            FakeEngine(),
            image,
            OcrPipelineProfile(name="test"),
        )
    finally:
        image.close()

    assert parts == ["projector slide text"]
    assert chunks == 1
    assert cards == 0
    assert calls == [((2000, 1200), "text_mode", 3)]


def test_sparse_easyocr_region_uses_profile_tesseract_fallback(monkeypatch):
    calls = []

    class PrimaryEngine:
        def info(self):
            return {"engine": "easyocr"}

        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(("primary", image.size, mode, psm))
            return "weak"

    class FallbackEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(("fallback", image.size, mode, psm))
            return (
                "Схема сбора статистической отчетности о работе судов "
                "Судебный департамент Федеральное хранилище судебной статистики "
                "Районные суды Мировые судьи"
            )

    monkeypatch.setattr(
        convert_service,
        "_create_sparse_text_fallback_engine",
        lambda profile: FallbackEngine(),
    )
    image = Image.new("RGB", (2000, 1200), "white")
    try:
        parts, chunks, cards = convert_service._recognize_image_region(
            PrimaryEngine(),
            image,
            OcrPipelineProfile(
                name="test",
                sparse_text_fallback_engine="tesseract",
                sparse_text_fallback_min_tokens=8,
                sparse_text_fallback_min_ratio=1.25,
            ),
        )
    finally:
        image.close()

    assert parts == [
        (
            "weak\n\n"
            "Схема сбора статистической отчетности о работе судов "
            "Судебный департамент Федеральное хранилище судебной статистики "
            "Районные суды Мировые судьи"
        )
    ]
    assert chunks == 1
    assert cards == 0
    assert calls == [
        ("primary", (2000, 1200), "text_mode", 3),
        ("fallback", (2000, 1200), "text_mode", 3),
    ]


def test_extra_pass_engine_uses_declared_recovery_engine(monkeypatch):
    class PrimaryEngine:
        def info(self):
            return {"engine": "easyocr"}

    recovery = object()
    monkeypatch.setattr(
        "app.engines.tesseract_engine.TesseractEngine",
        lambda **kwargs: (recovery, kwargs),
    )
    profile = OcrPipelineProfile(
        name="easyocr-with-recovery",
        sparse_text_fallback_engine="tesseract",
    )

    selected, kwargs = convert_service._extra_pass_engine(
        PrimaryEngine(),
        profile,
        language_priority=("rus", "eng"),
        ocr_border_pixels=0,
    )

    assert selected is recovery
    assert kwargs["language_priority"] == ("rus", "eng")
    assert kwargs["ocr_border_pixels"] == 0
    assert convert_service._engine_chain(PrimaryEngine(), profile) == [
        "easyocr",
        "tesseract",
    ]


def test_edge_word_uses_single_token_profile_fallback(monkeypatch):
    calls = []

    class PrimaryEngine:
        def info(self):
            return {"engine": "easyocr"}

        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(("primary", mode, psm))
            return ""

    class FallbackEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(("fallback", mode, psm))
            return "SAMPLE"

    monkeypatch.setattr(
        convert_service,
        "_create_sparse_text_fallback_engine",
        lambda profile: FallbackEngine(),
    )
    monkeypatch.setattr(
        convert_service,
        "_looks_like_edge_to_edge_word",
        lambda image: True,
    )
    image = Image.new("RGB", (3840, 2160), "white")
    try:
        markdown, meta = convert_service._convert_page_segment(
            image,
            PrimaryEngine(),
            OcrPipelineProfile(
                name="edge-word",
                sparse_text_fallback_engine="tesseract",
                sparse_text_fallback_min_tokens=18,
                edge_word_fallback_min_tokens=1,
            ),
        )
    finally:
        image.close()

    assert markdown == "SAMPLE"
    assert meta["chunks"] == 1
    assert calls == [
        ("primary", "text_mode", 3),
        ("fallback", "text_mode", 3),
    ]


def test_edge_word_bypasses_recursive_grid(monkeypatch):
    calls = []

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append((image.size, mode, psm))
            return "SAMPLE"

    def fail_layout(*_args, **_kwargs):
        raise AssertionError("edge words should bypass recursive grid")

    monkeypatch.setattr(convert_service, "analyze_layout", fail_layout)
    monkeypatch.setattr(
        convert_service,
        "_looks_like_edge_to_edge_word",
        lambda image: True,
    )
    profile = OcrPipelineProfile(
        name="edge-recursive",
        layout=LayoutPipelineConfig(
            allowed_stages=("recursive_grid",),
        ),
    )
    image = Image.new("RGB", (3840, 2160), "white")
    try:
        markdown, meta = convert_service._convert_page_segment(
            image,
            FakeEngine(),
            profile,
        )
    finally:
        image.close()

    assert markdown == "SAMPLE"
    assert "layout_decision:bypass_single_region" in meta["runtime_flags"]
    assert calls == [((3840, 2160), "text_mode", 3)]


def test_unconfirmed_sparse_grid_falls_back_to_plain_full_page_ocr(
    monkeypatch,
):
    calls = []

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(image.size)
            if image.size == (600, 240):
                return "first line\nsecond line"
            return "fragment"

    image = Image.new("RGB", (600, 240), "white")
    first = image.crop((0, 0, 600, 120))
    second = image.crop((0, 120, 600, 240))
    regions = [
        LayoutRegion(
            kind="image",
            image=first,
            bbox=(0, 0, 600, 120),
            metadata={
                "layout_kind": "recursive_grid_cell",
                "grid_row": 0,
                "grid_col": 0,
                "sparse_codes": ((0, 1, 5),),
            },
        ),
        LayoutRegion(
            kind="image",
            image=second,
            bbox=(0, 120, 600, 240),
            metadata={
                "layout_kind": "recursive_grid_cell",
                "grid_row": 1,
                "grid_col": 0,
                "sparse_codes": ((1, 0, 3),),
            },
        ),
    ]
    monkeypatch.setattr(
        convert_service,
        "_looks_like_edge_to_edge_word",
        lambda image: False,
    )
    monkeypatch.setattr(
        convert_service,
        "analyze_layout",
        lambda *_args, **_kwargs: (
            regions,
            LayoutDecision(
                label="fixed",
                stages=(
                    LayoutStageSpec(
                        name="recursive_grid",
                        parameters=(),
                    ),
                ),
                confidence=1.0,
            ),
        ),
    )
    profile = OcrPipelineProfile(
        name="plain-fallback",
        layout=LayoutPipelineConfig(
            allowed_stages=("recursive_grid",),
        ),
    )
    try:
        markdown, meta = convert_service._convert_page_segment(
            image,
            FakeEngine(),
            profile,
        )
    finally:
        image.close()

    assert markdown == "first line\nsecond line"
    assert "structural_grammar:bypass_unconfirmed_grid" in meta["runtime_flags"]
    assert "structural_plain_fallback:used" in meta["runtime_flags"]
    assert calls == [(600, 120), (600, 120), (600, 240)]


def test_confirmed_structural_markdown_blocks_plain_replacement(
    monkeypatch,
):
    calls = []

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            del mode, psm
            calls.append(image.size)
            if image.size == (600, 360):
                return "fallback full page"
            return "cell"

    image = Image.new("RGB", (600, 360), "white")
    unconfirmed = image.crop((0, 0, 600, 100))
    plain = image.crop((0, 100, 600, 140))
    confirmed = image.crop((0, 140, 600, 240))
    regions = [
        LayoutRegion(
            kind="image",
            image=unconfirmed,
            bbox=(0, 0, 600, 100),
            metadata={
                "layout_kind": "recursive_grid_cell",
                "grid_row": 0,
                "grid_col": 0,
                "sparse_codes": ((0, 1, 5),),
            },
        ),
        LayoutRegion(
            kind="image",
            image=plain,
            bbox=(0, 100, 600, 140),
        ),
        LayoutRegion(
            kind="image",
            image=confirmed,
            bbox=(0, 140, 600, 240),
            metadata={
                "layout_kind": "recursive_grid_cell",
                "grid_row": 2,
                "grid_col": 0,
                "sparse_codes": tuple((2, column, 5) for column in range(1, 5)),
            },
        ),
    ]
    monkeypatch.setattr(
        convert_service,
        "_looks_like_edge_to_edge_word",
        lambda image: False,
    )
    monkeypatch.setattr(
        convert_service,
        "analyze_layout",
        lambda *_args, **_kwargs: (
            regions,
            LayoutDecision(
                label="fixed",
                stages=(
                    LayoutStageSpec(
                        name="recursive_grid",
                        parameters=(),
                    ),
                ),
                confidence=1.0,
            ),
        ),
    )

    profile = OcrPipelineProfile(
        name="plain-fallback",
        layout=LayoutPipelineConfig(
            allowed_stages=("recursive_grid",),
        ),
    )
    try:
        markdown, meta = convert_service._convert_page_segment(
            image,
            FakeEngine(),
            profile,
        )
    finally:
        image.close()

    assert "fallback full page" not in markdown
    assert "structural_plain_fallback:used" not in meta["runtime_flags"]
    assert "structural_grammar:bypass_unconfirmed_grid" in meta["runtime_flags"]
    assert "structural_grammar:finite_merge_v1" in meta["runtime_flags"]
    assert calls == [(600, 100), (600, 40), (600, 100)]


def test_four_consecutive_merge_left_cells_confirm_sparse_table():
    rows = [
        SparseMarkdownRow(
            parts=("wide row",),
            anchor=(0, 0),
            codes=tuple((0, column, 5) for column in range(1, 5)),
        )
    ]

    assert convert_service._sparse_rows_have_confirmed_structure(rows)


def test_adjacent_tables_with_same_schema_merge_as_continuation():
    parts, merged = convert_service._merge_adjacent_compatible_table_parts(
        [
            "intro",
            ("| Index | Name |\n" "| --- | --- |\n" "| A | Alpha |"),
            ("| Index | Name |\n" "| --- | --- |\n" "| B | Beta |"),
            ("| Key | Value |\n" "| --- | --- |\n" "| C | Gamma |"),
        ]
    )

    assert merged == 1
    assert len(parts) == 3
    assert parts[1].count("| --- | --- |") == 1
    assert "| A | Alpha |" in parts[1]
    assert "| B | Beta |" in parts[1]
    assert parts[2].startswith("| Key | Value |")


def _synthetic_fragmented_long_card_parts() -> list[str]:
    def table(columns: int, body_rows: int, start: int) -> str:
        headers = [f"H{start + column}" for column in range(columns)]
        rows = [headers]
        value = start
        for _ in range(body_rows):
            row = []
            for _ in range(columns):
                suffix = " 1000 р" if value % 4 == 0 else ""
                row.append(f"Товар {value}{suffix}")
                value += 1
            rows.append(row)
        header = "| " + " | ".join(rows[0]) + " |"
        separator = "| " + " | ".join("---" for _ in rows[0]) + " |"
        body = ["| " + " | ".join(row) + " |" for row in rows[1:]]
        return "\n".join((header, separator, *body))

    return [
        "# spf 50",
        "== Защита от солнца Х Цена v Магазин",
        "По популярности",
        table(2, 7, 0),
        table(3, 7, 20),
        table(3, 5, 50),
    ]


def test_fragmented_long_card_grid_recovers_single_table():
    recovered = convert_service._recover_long_card_grid_table(_synthetic_fragmented_long_card_parts())

    assert recovered is not None
    assert recovered.startswith("# spf 50")
    assert "- Защита от солнца" in recovered
    assert "## Результаты" in recovered

    table_block = recovered.rsplit("\n\n", maxsplit=1)[-1]
    table = convert_service._markdown_table_part(table_block)
    assert table is not None
    header, _, body = table
    assert len(header) == 7
    assert len(body) == 22


def test_long_card_grid_recovery_keeps_existing_large_table():
    rows = [
        [f"H{column}" for column in range(7)],
        *[[f"Товар {row}-{column} 1000 р" for column in range(7)] for row in range(16)],
    ]
    table = "| " + " | ".join(rows[0]) + " |\n"
    table += "| " + " | ".join("---" for _ in rows[0]) + " |\n"
    table += "\n".join("| " + " | ".join(row) + " |" for row in rows[1:])

    assert convert_service._recover_long_card_grid_table([table]) is None


def test_long_screenshot_recovers_fragmented_card_grid(monkeypatch):
    image = Image.new("RGB", (100, 6000), "white")
    segments = (
        Image.new("RGB", (100, 1600), "white"),
        Image.new("RGB", (100, 1600), "white"),
        Image.new("RGB", (100, 520), "white"),
    )
    responses = iter(
        (
            (
                "\n\n".join(_synthetic_fragmented_long_card_parts()[:4]),
                {
                    "chunks": 1,
                    "cards_found": 0,
                    "tables_found": 1,
                    "table_cells": 16,
                    "runtime_flags": [
                        "structural_grammar:finite_merge_v1",
                        "markdown_lint:pass",
                    ],
                },
            ),
            (
                _synthetic_fragmented_long_card_parts()[4],
                {
                    "chunks": 1,
                    "cards_found": 0,
                    "tables_found": 1,
                    "table_cells": 24,
                    "runtime_flags": [
                        "structural_grammar:finite_merge_v1",
                        "markdown_lint:pass",
                    ],
                },
            ),
            (
                _synthetic_fragmented_long_card_parts()[5],
                {
                    "chunks": 1,
                    "cards_found": 0,
                    "tables_found": 1,
                    "table_cells": 18,
                    "runtime_flags": [
                        "structural_grammar:finite_merge_v1",
                        "markdown_lint:pass",
                    ],
                },
            ),
        )
    )

    monkeypatch.setattr(
        convert_service,
        "iter_vertical_segments",
        lambda *_args, **_kwargs: iter(segments),
    )
    monkeypatch.setattr(
        convert_service,
        "_convert_page_segment",
        lambda *_args, **_kwargs: next(responses),
    )

    try:
        markdown, meta = convert_service._convert_page(
            image,
            object(),
            OcrPipelineProfile(name="long"),
        )
    finally:
        image.close()
        for segment in segments:
            segment.close()

    assert markdown.count("| Бренд | Товар | Цена |") == 1
    assert markdown.count("| H0 | H1 |") == 0
    assert "long_card_grid_recovery:single_table" in meta["runtime_flags"]


def test_long_screenshot_merges_table_continuations_between_segments(monkeypatch):
    image = Image.new("RGB", (100, 6000), "white")
    segments = (
        Image.new("RGB", (100, 1600), "white"),
        Image.new("RGB", (100, 520), "white"),
    )
    responses = iter(
        [
            (
                ("intro\n\n" "| Index | Name |\n" "| --- | --- |\n" "| A | Alpha |"),
                {
                    "chunks": 1,
                    "cards_found": 0,
                    "tables_found": 1,
                    "table_cells": 4,
                    "runtime_flags": [
                        "structural_grammar:finite_merge_v1",
                        "markdown_lint:pass",
                    ],
                },
            ),
            (
                ("| Index | Name |\n" "| --- | --- |\n" "| B | Beta |\n\n" "tail"),
                {
                    "chunks": 1,
                    "cards_found": 0,
                    "tables_found": 1,
                    "table_cells": 4,
                    "runtime_flags": [
                        "structural_grammar:finite_merge_v1",
                        "markdown_lint:pass",
                    ],
                },
            ),
        ]
    )

    monkeypatch.setattr(
        convert_service,
        "iter_vertical_segments",
        lambda *_args, **_kwargs: iter(segments),
    )
    monkeypatch.setattr(
        convert_service,
        "_convert_page_segment",
        lambda *_args, **_kwargs: next(responses),
    )

    markdown, meta = convert_service._convert_page(
        image,
        object(),
        OcrPipelineProfile(name="long"),
    )

    assert markdown.count("| Index | Name |") == 1
    assert "| A | Alpha |" in markdown
    assert "| B | Beta |" in markdown
    assert "structural_grammar:merge_table_continuations" in meta["runtime_flags"]


def test_dewarped_projector_slide_bypasses_spatial_layout(monkeypatch):
    calls = []

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append((image.size, mode, psm))
            return "full projector slide text"

    def fail_layout(*_args, **_kwargs):
        raise AssertionError("dewarped projector slides should not be split")

    monkeypatch.setattr(convert_service, "analyze_layout", fail_layout)
    profile = OcrPipelineProfile(
        name="projector-spatial",
        layout=LayoutPipelineConfig(
            feature_extractors=("projection_geometry",),
            selector="uniform_spatial_v1",
            allowed_stages=("spatial_regions",),
        ),
    )
    image = Image.new("RGB", (2000, 1200), "white")
    try:
        markdown, meta = convert_service._convert_page_segment(
            image,
            FakeEngine(),
            profile,
        )
    finally:
        image.close()

    assert markdown == "full projector slide text"
    assert meta["chunks"] == 1
    assert calls == [((2000, 1200), "text_mode", 3)]


def _table_fixture() -> Image.Image:
    image = Image.new("RGB", (420, 220), "white")
    draw = ImageDraw.Draw(image)

    for x in [20, 220, 400]:
        draw.line((x, 20, x, 200), fill="black", width=3)
    for y in [20, 110, 200]:
        draw.line((20, y, 400, y), fill="black", width=3)

    for x, y in [(60, 58), (260, 58), (60, 148), (260, 148)]:
        draw.text((x, y), "x", fill="black")

    return image


def _noisy_table_fixture() -> Image.Image:
    rng = np.random.default_rng(42)
    base = np.full((240, 460, 3), 244, dtype=np.uint8)
    noise = rng.integers(0, 18, size=base.shape, dtype=np.uint8)
    base = np.clip(base - noise, 0, 255).astype(np.uint8)
    image = Image.fromarray(base).convert("RGB")
    draw = ImageDraw.Draw(image)

    for x in [25, 180, 315, 435]:
        draw.line((x, 30, x, 215), fill="black", width=2)
    for y in [30, 88, 148, 215]:
        draw.line((25, y, 435, y), fill="black", width=2)
    draw.text((45, 52), "Subject", fill="black")
    draw.text((205, 52), "Hours", fill="black")
    draw.text((335, 52), "Code", fill="black")
    draw.text((45, 112), "Math", fill="black")
    draw.text((205, 112), "42", fill="black")

    return image


def _hierarchical_indent_table_fixture() -> Image.Image:
    image = Image.new("RGB", (560, 230), "white")
    draw = ImageDraw.Draw(image)

    for x in [20, 160, 320, 540]:
        draw.line((x, 20, x, 210), fill="black", width=3)
    for y in [20, 60, 100, 140, 180, 210]:
        draw.line((20, y, 540, y), fill="black", width=3)

    # Short hierarchy stroke inside the first logical column. It should not
    # become its own Markdown column.
    draw.line((70, 100, 70, 210), fill="black", width=3)

    draw.text((60, 34), "Index", fill="black")
    draw.text((210, 34), "Name", fill="black")
    draw.text((390, 34), "Competencies", fill="black")
    draw.text((35, 113), "B1.O.01", fill="black")
    draw.text((180, 113), "Math", fill="black")
    draw.text((340, 113), "UK-1", fill="black")

    return image


def _bar_chart_fixture() -> Image.Image:
    image = Image.new("RGB", (900, 700), (8, 32, 92))
    draw = ImageDraw.Draw(image)

    draw.text((30, 30), "Top devices", fill="white")
    for index in range(10):
        top = 100 + index * 52
        right = 790 - index * 24
        draw.rectangle((90, top, right, top + 36), fill=(20, 120 + index * 8, 240))
        draw.text((40, top + 8), str(index + 1), fill="white")
        draw.text((105, top + 8), f"Device {index + 1}", fill="white")
        draw.text((right + 8, top + 8), str(1_800_000 - index * 90_000), fill="white")

    return image


def _dense_table_layout(width: int, height: int, *, rows: int, cols: int) -> TableLayout:
    x_lines = tuple(round(index * (width - 1) / cols) for index in range(cols + 1))
    y_lines = tuple(round(index * (height - 1) / rows) for index in range(rows + 1))
    cells = tuple(
        TableCell(
            row=row,
            col=col,
            bbox=(x_lines[col], y_lines[row], x_lines[col + 1], y_lines[row + 1]),
        )
        for row in range(rows)
        for col in range(cols)
    )
    return TableLayout(
        bbox=(0, 0, width, height),
        rows=rows,
        cols=cols,
        x_lines=x_lines,
        y_lines=y_lines,
        cells=cells,
    )


def test_recursive_table_uses_micro_cell_ocr_above_generic_budget():
    calls = []

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append((image.size, mode, psm))
            return f"cell-{len(calls)}"

    image = Image.new("RGB", (250, 250), (200, 200, 200))
    draw = ImageDraw.Draw(image)
    for row in range(5):
        for col in range(5):
            x = col * 50 + 25
            y = row * 50 + 20
            draw.line((x, y, x, y + 10), fill="black", width=1)
    layout = _dense_table_layout(250, 250, rows=5, cols=5)
    region = LayoutRegion(
        kind="table",
        image=image,
        bbox=layout.bbox,
        table=layout,
        metadata={"layout_kind": "recursive_grid_table"},
    )
    try:
        parts, meta = convert_service._convert_layout_region(
            region,
            FakeEngine(),
            OcrPipelineProfile(
                name="recursive-cells",
                max_table_cell_ocr_calls=1,
                recursive_table_cell_ocr="auto",
                recursive_table_cell_ocr_batch_pixels=8_000_000,
            ),
        )
    finally:
        image.close()

    assert len(calls) == 25
    assert "cell-1" in parts[0]
    assert "cell-25" in parts[0]
    assert meta["chunks"] == 25
    assert "ocr_region_micro_cells:recursive_grid" in meta["runtime_flags"]
    assert "ocr_region_micro_cells:batch_pixels=8000000" in meta["runtime_flags"]
    assert "ocr_region_micro_cells:selected" in meta["runtime_flags"]


def test_recursive_table_large_grid_recovers_only_missing_visible_cells():
    from app.recognition.segments import (
        recognize_table_cell_candidate,
        should_try_recursive_table_cells,
    )

    batch_calls = []

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=0):
            batch_calls.append((image.size, psm, min_conf))
            return [
                {"text": "7", "bbox": (30, 30, 70, 70), "conf": 95},
                {"text": "8", "bbox": (142, 30, 182, 70), "conf": 94},
            ]

    image = Image.new("RGB", (400, 400), "white")
    draw = ImageDraw.Draw(image)
    layout = _dense_table_layout(400, 400, rows=20, cols=20)
    draw.text((4, 4), "seed", fill="black")
    draw.text((24, 4), "7", fill="black")
    draw.text((4, 24), "8", fill="black")
    seed_words = [
        {
            "text": "seed",
            "bbox": (2, 2, 18, 18),
            "conf": 95,
        }
    ]

    assert should_try_recursive_table_cells(
        metadata={"layout_kind": "recursive_grid_table"},
        mode="auto",
        cell_count=len(layout.cells),
        table_markdown="| seed |",
        word_cell_coverage=0.9,
        min_word_cell_coverage=0.35,
    )

    try:
        candidate = recognize_table_cell_candidate(
            FakeEngine(),
            image,
            layout,
            seed_words=seed_words,
            max_batch_pixels=8_000_000,
        )
    finally:
        image.close()

    assert len(batch_calls) == 1
    batch_size, batch_psm, batch_min_conf = batch_calls[0]
    assert batch_size[0] == 1800
    assert batch_size[1] <= 8_000_000 // batch_size[0]
    assert (batch_psm, batch_min_conf) == (11, 5)
    assert candidate.calls == 1
    assert candidate.added_cells == 2
    assert candidate.rows[0][0] == "seed"
    assert {word["text"] for word in candidate.recovered_words} == {"7", "8"}


def test_recursive_table_micro_cell_ocr_can_be_disabled():
    calls = []

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append((image.size, mode, psm))
            return "raw fallback"

    image = Image.new("RGB", (250, 250), (200, 200, 200))
    layout = _dense_table_layout(250, 250, rows=5, cols=5)
    region = LayoutRegion(
        kind="table",
        image=image,
        bbox=layout.bbox,
        table=layout,
        metadata={"layout_kind": "recursive_grid_table"},
    )
    try:
        parts, meta = convert_service._convert_layout_region(
            region,
            FakeEngine(),
            OcrPipelineProfile(
                name="recursive-cells-off",
                max_table_cell_ocr_calls=1,
                recursive_table_cell_ocr="off",
                recursive_table_cell_ocr_batch_pixels=8_000_000,
            ),
        )
    finally:
        image.close()

    assert parts == ["raw fallback"]
    assert len(calls) == 1
    assert "ocr_region_micro_cells:recursive_grid" not in meta["runtime_flags"]


def test_wide_grid_selects_curriculum_table_processing_plan():
    from app.layout.table_formatters import select_table_processing_plan

    plan = select_table_processing_plan(
        _dense_table_layout(2000, 900, rows=20, cols=90),
        layout_normalization="logical_columns",
        word_recognition="bounded_tiles",
        formatter_names=("generic_markdown",),
    )

    assert plan.layout_normalization == "preserve_grid"
    assert plan.word_recognition == "single_pass_with_left_strip"
    assert plan.formatter_names == ("curriculum", "generic_markdown")
    assert plan.reason == "wide_curriculum_grid"


def test_regular_grid_keeps_requested_table_processing_plan():
    from app.layout.table_formatters import select_table_processing_plan

    plan = select_table_processing_plan(
        _dense_table_layout(800, 500, rows=10, cols=14),
        layout_normalization="logical_columns",
        word_recognition="bounded_tiles",
        formatter_names=("generic_markdown",),
    )

    assert plan.layout_normalization == "logical_columns"
    assert plan.word_recognition == "bounded_tiles"
    assert plan.formatter_names == ("generic_markdown",)
    assert plan.reason == ""


def test_detect_table_layouts_finds_grid_cells():
    pytest.importorskip("cv2")
    layouts = detect_table_layouts(_table_fixture())

    assert len(layouts) == 1
    assert layouts[0].rows == 2
    assert layouts[0].cols == 2
    assert len(layouts[0].cells) == 4


def test_logical_table_layout_collapses_hierarchy_indent_lines():
    pytest.importorskip("cv2")
    image = _hierarchical_indent_table_fixture()
    layout = detect_table_layouts(image)[0]

    logical = logical_table_layout(image, layout)
    markdown = table_words_to_markdown(
        logical,
        [
            {"text": "Index", "bbox": (60, 34, 110, 50), "conf": 98},
            {"text": "Name", "bbox": (210, 34, 260, 50), "conf": 98},
            {"text": "Competencies", "bbox": (390, 34, 490, 50), "conf": 98},
            {"text": "B1.O.01", "bbox": (35, 113, 95, 130), "conf": 98},
            {"text": "Math", "bbox": (180, 113, 230, 130), "conf": 98},
            {"text": "UK-1", "bbox": (340, 113, 380, 130), "conf": 98},
        ],
    )

    assert logical.cols == 3
    assert "| Index | Name | Competencies |" in markdown
    assert "| B1.O.01 | Math | UK-1 |" in markdown


def test_table_layout_to_markdown_ocr_cells_in_reading_order():
    pytest.importorskip("cv2")
    layout = detect_table_layouts(_table_fixture())[0]
    texts = iter(["Предмет", "Часы", "Math", "42"])

    markdown = table_layout_to_markdown(_table_fixture(), layout, lambda _cell: next(texts))

    assert markdown == "| Предмет | Часы |\n| --- | --- |\n| Math | 42 |"


def test_table_words_to_markdown_maps_ocr_words_to_cells():
    pytest.importorskip("cv2")
    layout = detect_table_layouts(_table_fixture())[0]
    words = [
        {"text": "Предмет", "bbox": (55, 52, 120, 75), "conf": 98},
        {"text": "Часы", "bbox": (255, 52, 310, 75), "conf": 98},
        {"text": "Math", "bbox": (55, 142, 110, 165), "conf": 98},
        {"text": "42", "bbox": (255, 142, 285, 165), "conf": 98},
    ]

    markdown = table_words_to_markdown(layout, words)

    assert markdown == "| Предмет | Часы |\n| --- | --- |\n| Math | 42 |"


def test_prepare_cell_for_ocr_inverts_dark_cell_background():
    image = Image.new("RGB", (100, 60), (25, 70, 55))
    draw = ImageDraw.Draw(image)
    draw.text((14, 18), "Text", fill="white")

    prepared = _prepare_cell_for_ocr(image)

    try:
        assert prepared.getpixel((0, 0)) == 255
        assert prepared.getpixel((prepared.width - 12, prepared.height - 12)) > 170
    finally:
        prepared.close()
        image.close()


def test_table_words_to_markdown_normalizes_curriculum_index_column():
    pytest.importorskip("cv2")
    layout = detect_table_layouts(_table_fixture())[0]
    words = [
        {"text": "Индекс", "bbox": (55, 52, 120, 75), "conf": 98},
        {"text": "Наименование", "bbox": (255, 52, 350, 75), "conf": 98},
        {"text": "51.0.01", "bbox": (55, 142, 120, 165), "conf": 98},
        {"text": "Иностранный", "bbox": (255, 142, 340, 165), "conf": 98},
    ]

    markdown = table_words_to_markdown(layout, words)

    assert "| Б1.О.01 | Иностранный |" in markdown


def test_wide_table_word_recognition_uses_sparse_psm():
    image = Image.new("RGB", (900, 500), "white")
    layout = _dense_table_layout(900, 500, rows=8, cols=6)
    observed_psms = []

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            observed_psms.append(psm)
            return []

    convert_service._recognize_table_words(
        FakeEngine(),
        image,
        layout,
        OcrPipelineProfile(name="test"),
    )

    assert observed_psms == [11]


def test_narrow_table_word_recognition_keeps_block_psm():
    image = Image.new("RGB", (400, 220), "white")
    layout = _dense_table_layout(400, 220, rows=3, cols=2)
    observed_psms = []

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            observed_psms.append(psm)
            return []

    convert_service._recognize_table_words(
        FakeEngine(),
        image,
        layout,
        OcrPipelineProfile(name="test"),
    )

    assert observed_psms == [6]


def test_mixed_10x14_table_keeps_placeholder_cells_and_raw_fallback():
    image = Image.new("RGB", (1000, 1400), "white")
    draw = ImageDraw.Draw(image)
    for y in range(0, 1401, 100):
        draw.line((0, min(y, 1399), 999, min(y, 1399)), fill="black", width=2)
    for x in range(0, 1001, 100):
        bounded_x = min(x, 999)
        draw.line((bounded_x, 0, bounded_x, 200), fill="black", width=2)
        draw.line((bounded_x, 300, bounded_x, 1399), fill="black", width=2)
    layout = _dense_table_layout(1000, 1400, rows=14, cols=10)
    words = []
    for col in range(10):
        left = col * 100 + 10
        words.append(
            {
                "text": f"H{col + 1}",
                "bbox": (left, 20, left + 40, 50),
                "conf": 98,
            }
        )
    words.extend(
        [
            {"text": "й-A1-EN-001", "bbox": (110, 125, 190, 150), "conf": 98},
            {"text": "Привет", "bbox": (210, 125, 270, 150), "conf": 98},
            {"text": "Sample", "bbox": (310, 125, 370, 150), "conf": 98},
            {"text": "中文", "bbox": (410, 125, 450, 150), "conf": 98},
            {
                "text": "РАЗДЕЛ A SECTION ALPHA merged subsection й-ALPHA-2026",
                "bbox": (10, 225, 990, 255),
                "conf": 98,
            },
        ]
    )

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            assert psm == 11
            return words

        def recognize(self, image, mode="text_mode", psm=6):
            assert psm == 11
            return "raw mixed fallback 中文 Fake blocks 909"

    profile = OcrPipelineProfile(
        name="test",
        table_layout_normalization="preserve_grid",
        table_min_word_cell_coverage=0.0,
        table_raw_text_fallback=True,
        table_raw_text_fallback_min_rows=10,
        table_raw_text_fallback_min_cols=8,
        table_raw_text_fallback_max_cols=14,
    )
    parts, meta = convert_service._convert_layout_region(
        LayoutRegion(kind="table", image=image, bbox=layout.bbox, table=layout),
        FakeEngine(),
        profile,
    )

    markdown = "\n\n".join(parts)
    table_lines = [line for line in markdown.splitlines() if line.startswith("|")]
    section_line = next(line for line in table_lines if "РАЗДЕЛ A SECTION ALPHA" in line)

    assert len(section_line.strip()[1:-1].split("|")) == 10
    assert section_line.startswith("| РАЗДЕЛ A SECTION ALPHA merged subsection й-ALPHA-2026 |")
    assert "::merge-left::" not in section_line
    assert "table_slot_builder:auto_line_merge_v1" in meta["runtime_flags"]
    assert "raw mixed fallback 中文 Fake blocks 909" in markdown
    assert meta["tables_found"] == 1
    assert meta["table_cells"] == 140


def test_table_slot_builder_does_not_append_duplicate_raw_fallback():
    table = _dense_table_layout(1000, 1400, rows=14, cols=10)
    profile = OcrPipelineProfile(
        name="test",
        table_slot_builder="recursive_gaps_v1",
        table_raw_text_fallback=True,
        table_raw_text_fallback_min_rows=10,
        table_raw_text_fallback_min_cols=8,
        table_raw_text_fallback_max_cols=14,
    )

    assert not convert_service._should_append_table_raw_text_after_markdown(
        profile,
        table,
        word_cell_coverage=0.2,
    )


def test_sparse_table_raw_fallback_skips_high_overlap_ocr_copy():
    page_parts = [
        "| Индекс | Наименование | Часы |\n" "| --- | --- | --- |\n" "| Б1.О.01 | Математический анализ | 252 |"
    ]

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            return "Индекс Наименование Часы\n" "Б1.О.01 Математический анализ 252\n" "Незначительный шум"

    calls = convert_service._append_sparse_table_raw_fallback(
        page_parts,
        FakeEngine(),
        Image.new("RGB", (300, 100), "white"),
        OcrPipelineProfile(name="test"),
    )

    assert calls == 1
    assert len(page_parts) == 1


def test_table_slot_builder_uses_missing_vertical_lines_for_merge_cells():
    image = Image.new("RGB", (300, 300), "white")
    draw = ImageDraw.Draw(image)
    for y in (0, 100, 200, 299):
        draw.line((0, y, 299, y), fill="black", width=2)
    for x in (0, 299):
        draw.line((x, 0, x, 299), fill="black", width=2)
    for x in (100, 200):
        draw.line((x, 0, x, 100), fill="black", width=2)
        draw.line((x, 200, x, 299), fill="black", width=2)

    layout = _dense_table_layout(300, 300, rows=3, cols=3)
    words = [
        {"text": "A", "bbox": (20, 30, 40, 50), "conf": 98},
        {"text": "B", "bbox": (120, 30, 140, 50), "conf": 98},
        {"text": "C", "bbox": (220, 30, 240, 50), "conf": 98},
        {"text": "Section", "bbox": (20, 130, 280, 150), "conf": 98},
        {"text": "1", "bbox": (20, 230, 40, 250), "conf": 98},
        {"text": "2", "bbox": (120, 230, 140, 250), "conf": 98},
        {"text": "3", "bbox": (220, 230, 240, 250), "conf": 98},
    ]

    markdown = table_words_to_slot_markdown(image, layout, words)

    assert "| Section |  |  |" in markdown
    assert "::merge-left::" not in markdown
    assert "| 1 | 2 | 3 |" in markdown


def test_table_slot_markdown_normalizes_curriculum_index_after_superheader():
    image = Image.new("RGB", (400, 250), "white")
    layout = _dense_table_layout(400, 250, rows=5, cols=4)

    def word(row: int, col: int, text: str) -> dict:
        left = layout.x_lines[col] + 8
        top = layout.y_lines[row] + 10
        return {
            "text": text,
            "bbox": (left, top, min(left + 70, layout.x_lines[col + 1] - 4), top + 18),
            "conf": 98,
        }

    words = [
        word(0, 2, "="),
        word(1, 0, "Индекс"),
        word(1, 1, "Наименование"),
        word(1, 2, "Форма контроля"),
        word(1, 3, "Кафедра"),
        word(2, 0, "51.0.01"),
        word(2, 1, "Математика"),
        word(3, 0, "151.0.01.07"),
        word(3, 1, "Теория графов"),
        word(4, 0, "1Б1.В.ДВ.02"),
        word(4, 1, "Элективные дисциплины"),
    ]

    markdown = table_words_to_slot_markdown(image, layout, words, mode="off")

    assert "| 51.0.01 |" not in markdown
    assert "| 151.0.01.07 |" not in markdown
    assert "| 1Б1.В.ДВ.02 |" not in markdown
    assert "| Б1.О.01 | Математика |" in markdown
    assert "| Б1.О.01.07 | Теория графов |" in markdown
    assert "| Б1.В.ДВ.02 | Элективные дисциплины |" in markdown


def test_recursive_gap_slot_builder_adds_virtual_boundaries_from_gaps():
    image = Image.new("RGB", (400, 120), "white")
    layout = TableLayout(
        bbox=(0, 0, 400, 120),
        rows=1,
        cols=1,
        x_lines=(0, 400),
        y_lines=(0, 120),
        cells=(TableCell(row=0, col=0, bbox=(0, 0, 400, 120)),),
    )
    words = [
        {"text": "Индекс", "bbox": (20, 40, 75, 60), "conf": 98},
        {"text": "Наименование", "bbox": (180, 40, 300, 60), "conf": 98},
    ]

    markdown = table_words_to_slot_markdown(
        image,
        layout,
        words,
        mode="recursive_gaps_v1",
    )

    assert "| Индекс | Наименование |" in markdown


def test_recursive_gap_slot_builder_marks_crossed_text_as_fake_cells():
    image = Image.new("RGB", (300, 200), "white")
    draw = ImageDraw.Draw(image)
    for y in (0, 100, 199):
        draw.line((0, y, 299, y), fill="black", width=2)
    for x in (0, 100, 200, 299):
        draw.line((x, 0, x, 99), fill="black", width=2)
        draw.line((x, 100, x, 199), fill="black", width=2)
    layout = _dense_table_layout(300, 200, rows=2, cols=3)
    words = [
        {"text": "РАЗДЕЛ A SECTION", "bbox": (20, 35, 280, 60), "conf": 98},
        {"text": "1", "bbox": (20, 130, 40, 150), "conf": 98},
        {"text": "2", "bbox": (120, 130, 140, 150), "conf": 98},
        {"text": "3", "bbox": (220, 130, 240, 150), "conf": 98},
    ]

    markdown = table_words_to_slot_markdown(
        image,
        layout,
        words,
        mode="recursive_gaps_v1",
    )

    assert "| РАЗДЕЛ A SECTION |  |  |" in markdown
    assert "Column 2" not in markdown
    assert "::merge-left::" not in markdown
    assert "| 1 | 2 | 3 |" in markdown


def test_recursive_gap_slot_builder_keeps_blank_cells_blank_not_merge_up():
    image = Image.new("RGB", (300, 200), "white")
    draw = ImageDraw.Draw(image)
    for y in (0, 100, 199):
        draw.line((0, y, 299, y), fill="black", width=2)
    for x in (0, 100, 200, 299):
        draw.line((x, 0, x, 199), fill="black", width=2)
    layout = _dense_table_layout(300, 200, rows=2, cols=3)
    words = [
        {"text": "A", "bbox": (20, 35, 40, 60), "conf": 98},
        {"text": "B", "bbox": (120, 35, 140, 60), "conf": 98},
        {"text": "C", "bbox": (220, 35, 240, 60), "conf": 98},
        {"text": "1", "bbox": (20, 130, 40, 150), "conf": 98},
        {"text": "3", "bbox": (220, 130, 240, 150), "conf": 98},
    ]

    markdown = table_words_to_slot_markdown(
        image,
        layout,
        words,
        mode="recursive_gaps_v1",
    )

    assert "| 1 |  | 3 |" in markdown
    assert "::merge-up::" not in markdown


def test_recursive_word_grid_builds_table_without_fixed_shape():
    words = [
        {"text": "A", "bbox": (10, 10, 20, 25), "conf": 98},
        {"text": "B", "bbox": (100, 10, 110, 25), "conf": 98},
        {"text": "C", "bbox": (190, 10, 200, 25), "conf": 98},
        {"text": "1", "bbox": (10, 45, 20, 60), "conf": 98},
        {"text": "2", "bbox": (100, 45, 110, 60), "conf": 98},
        {"text": "3", "bbox": (190, 45, 200, 60), "conf": 98},
        {"text": "4", "bbox": (10, 80, 20, 95), "conf": 98},
        {"text": "5", "bbox": (100, 80, 110, 95), "conf": 98},
        {"text": "6", "bbox": (190, 80, 200, 95), "conf": 98},
    ]

    markdown = words_to_recursive_slot_markdown(words)

    assert "| A | B | C |" in markdown
    assert "| 1 | 2 | 3 |" in markdown
    assert "| 4 | 5 | 6 |" in markdown


def test_recursive_word_grid_rejects_plain_prose():
    words = [
        {"text": "ordinary", "bbox": (10, 10, 70, 25), "conf": 98},
        {"text": "paragraph", "bbox": (80, 10, 150, 25), "conf": 98},
        {"text": "without", "bbox": (160, 10, 220, 25), "conf": 98},
        {"text": "table", "bbox": (230, 10, 270, 25), "conf": 98},
        {"text": "geometry", "bbox": (280, 10, 350, 25), "conf": 98},
    ]

    assert words_to_recursive_slot_markdown(words) == ""


def test_recursive_word_grid_rejects_multiline_prose_pseudo_columns():
    lines = [
        ["ordinary", "documentation", "paragraph", "with", "several", "wide", "gaps"],
        ["features", "are", "listed", "in", "text", "before", "tables"],
        ["local", "backend", "uses", "tesseract", "and", "easyocr", "profiles"],
        ["browser", "worker", "stays", "inside", "the", "current", "tab"],
        ["markdown", "output", "must", "not", "become", "fake", "grid"],
        ["security", "notes", "describe", "memory", "limits", "and", "cleanup"],
    ]
    words = []
    for row, line in enumerate(lines):
        x = 10
        for index, text in enumerate(line):
            width = 30 + len(text) * 5
            words.append(
                {
                    "text": text,
                    "bbox": (x, 10 + row * 28, x + width, 25 + row * 28),
                    "conf": 98,
                }
            )
            x += width + (35 if index % 2 == 0 else 18)

    assert words_to_recursive_slot_markdown(words, max_cols=14) == ""


def test_recursive_word_grid_rejects_sparse_overwide_noise():
    words = [
        {"text": f"w{index}", "bbox": (index * 40, index * 18, index * 40 + 12, index * 18 + 12), "conf": 98}
        for index in range(12)
    ]

    assert words_to_recursive_slot_markdown(words) == ""


def test_wide_easyocr_table_raw_fallback_uses_sparse_tesseract(monkeypatch):
    image = Image.new("RGB", (2600, 1000), "white")
    layout = _dense_table_layout(2600, 1000, rows=10, cols=26)
    words = [
        {
            "text": f"Б1.О.{index:02d}",
            "bbox": (index * 90 + 8, 18, index * 90 + 74, 42),
            "conf": 95,
        }
        for index in range(26)
    ]
    calls = []

    class PrimaryEngine:
        def info(self):
            return {"engine": "easyocr"}

        def recognize_words(self, image, psm=6, min_conf=20):
            return words

        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(("primary", psm))
            return "weak"

    class FallbackEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(("fallback", psm))
            return (
                "Математический анализ Линейная алгебра "
                "Дифференциальные уравнения Дискретная математика "
                "Теория вероятностей Методы оптимизации Теория управления"
            )

    monkeypatch.setattr(
        convert_service,
        "_create_sparse_text_fallback_engine",
        lambda profile: FallbackEngine(),
    )

    profile = OcrPipelineProfile(
        name="test",
        table_layout_normalization="preserve_grid",
        table_min_word_cell_coverage=0.0,
        table_raw_text_fallback=True,
        table_raw_text_fallback_min_rows=10,
        table_raw_text_fallback_min_cols=8,
        table_raw_text_fallback_max_cols=30,
        table_raw_text_fallback_psm=11,
        sparse_text_fallback_engine="tesseract",
        sparse_text_fallback_min_tokens=6,
        sparse_text_fallback_min_ratio=1.25,
    )
    parts, meta = convert_service._convert_layout_region(
        LayoutRegion(kind="table", image=image, bbox=layout.bbox, table=layout),
        PrimaryEngine(),
        profile,
    )

    markdown = "\n\n".join(parts)

    assert "Дифференциальные уравнения" in markdown
    assert calls == [("primary", 11), ("fallback", 11)]
    assert meta["chunks"] >= 2
    assert meta["tables_found"] == 1


def test_wide_easyocr_table_without_markdown_uses_table_raw_sparse_fallback(
    monkeypatch,
):
    image = Image.new("RGB", (2600, 1000), "white")
    layout = _dense_table_layout(2600, 1000, rows=10, cols=26)
    calls = []

    class PrimaryEngine:
        def info(self):
            return {"engine": "easyocr"}

        def recognize_words(self, image, psm=6, min_conf=20):
            return []

        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(("primary", psm))
            return "weak"

    class FallbackEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(("fallback", psm))
            return (
                "Математический анализ Линейная алгебра "
                "Дифференциальные уравнения Дискретная математика "
                "Теория вероятностей Методы оптимизации Теория управления"
            )

    monkeypatch.setattr(
        convert_service,
        "_create_sparse_text_fallback_engine",
        lambda profile: FallbackEngine(),
    )

    profile = OcrPipelineProfile(
        name="test",
        table_layout_normalization="preserve_grid",
        table_raw_text_fallback=True,
        table_raw_text_fallback_min_rows=10,
        table_raw_text_fallback_min_cols=8,
        table_raw_text_fallback_max_cols=30,
        table_raw_text_fallback_min_ratio=0.75,
        table_raw_text_fallback_psm=11,
        sparse_text_fallback_engine="tesseract",
        sparse_text_fallback_min_tokens=6,
    )
    parts, meta = convert_service._convert_layout_region(
        LayoutRegion(kind="table", image=image, bbox=layout.bbox, table=layout),
        PrimaryEngine(),
        profile,
    )

    markdown = "\n\n".join(parts)

    assert "Дифференциальные уравнения" in markdown
    assert calls == [("primary", 11), ("fallback", 11)]
    assert meta["chunks"] >= 2
    assert meta["tables_found"] == 1


def test_wide_curriculum_table_markdown_repairs_ocr_index_noise():
    x_lines = tuple(index * 24 for index in range(51))
    y_lines = tuple(index * 24 for index in range(8))
    layout = TableLayout(
        bbox=(0, 0, x_lines[-1], y_lines[-1]),
        rows=len(y_lines) - 1,
        cols=len(x_lines) - 1,
        x_lines=x_lines,
        y_lines=y_lines,
        cells=(),
    )
    words = [
        {"text": "Индекс", "bbox": (2, 50, 40, 64), "conf": 95},
        {"text": "Наименование", "bbox": (28, 50, 44, 64), "conf": 95},
        {"text": "Блок 1.Дисциплины", "bbox": (2, 74, 20, 88), "conf": 95},
        {"text": "(модули)", "bbox": (28, 74, 44, 88), "conf": 95},
        {"text": "Обязательная часть", "bbox": (2, 98, 20, 112), "conf": 95},
        {"text": "61.0.01", "bbox": (2, 122, 20, 136), "conf": 95},
        {"text": "Иностранный язык", "bbox": (28, 122, 44, 136), "conf": 95},
        {"text": "Б61.0.02", "bbox": (2, 146, 20, 160), "conf": 95},
        {"text": "История", "bbox": (28, 146, 44, 160), "conf": 95},
    ]

    markdown = wide_curriculum_table_to_markdown(layout, words)

    assert "| Б1 | Дисциплины (модули) |" in markdown
    assert "| Б1.О | Обязательная часть |" in markdown
    assert "| Б1.О.01 | Иностранный язык |" in markdown
    assert "| Б1.О.02 | История |" in markdown


def test_wide_curriculum_table_repairs_merged_and_missing_sequence_rows():
    def curriculum_layout_and_words(pairs):
        x_lines = tuple(index * 24 for index in range(51))
        y_lines = tuple(index * 24 for index in range(len(pairs) + 1))
        layout = TableLayout(
            bbox=(0, 0, x_lines[-1], y_lines[-1]),
            rows=len(y_lines) - 1,
            cols=len(x_lines) - 1,
            x_lines=x_lines,
            y_lines=y_lines,
            cells=(),
        )
        words = []
        for row_index, (index_text, name_text) in enumerate(pairs):
            top = row_index * 24 + 2
            words.extend(
                [
                    {"text": index_text, "bbox": (2, top, 20, top + 14), "conf": 95},
                    {"text": name_text, "bbox": (28, top, 44, top + 14), "conf": 95},
                ]
            )
        return layout, words

    layout, words = curriculum_layout_and_words(
        [
            ("510.19", "Алгоритмы и анализ сложности"),
            ("ROM", "Базы данных"),
            ("Бї 0.21", "Теория графов"),
            ("61.0.2", "Алгебраические структуры"),
            ("510.23", "Современные средства"),
            ("51 2.24 510.25", "Нейронные сети Тестирование ПО"),
        ]
    )

    markdown = wide_curriculum_table_to_markdown(layout, words)

    assert "| Б1.О.20 | Базы данных |" in markdown
    assert "| Б1.О.21 | Теория графов |" in markdown
    assert "| Б1.О.22 | Алгебраические структуры |" in markdown
    assert "| Б1.О.24 | Нейронные сети |" in markdown
    assert "| Б1.О.25 | Тестирование ПО |" in markdown

    layout, words = curriculum_layout_and_words(
        [
            ("=“. 518.02", "Практикум на ЭВМ"),
            ("Еш", "Проектирование баз данных"),
            ("518.04", "Теория автоматов"),
        ]
    )

    markdown = wide_curriculum_table_to_markdown(layout, words)

    assert "| Б1.В | Часть, формируемая участниками образовательных отношений |" in markdown
    assert "| Б1.В.01 |  |" in markdown
    assert "| Б1.В.02 | Практикум на ЭВМ |" in markdown
    assert "| Б1.В.03 | Проектирование баз данных |" in markdown
    assert "| Б1.В.04 | Теория автоматов |" in markdown


def test_analyze_document_layout_returns_isolated_table_region():
    pytest.importorskip("cv2")
    regions = analyze_document_layout(_table_fixture())

    assert len(regions) == 1
    assert regions[0].kind == "table"
    assert regions[0].table is not None
    assert regions[0].table.bbox[0] == 0
    assert regions[0].table.rows == 2


def test_detect_table_layouts_handles_scan_like_noise():
    pytest.importorskip("cv2")
    layouts = detect_table_layouts(_noisy_table_fixture())

    assert len(layouts) == 1
    assert layouts[0].rows == 3
    assert layouts[0].cols == 3


def test_detect_table_layouts_ignores_text_contours_inside_generated_grid():
    pytest.importorskip("cv2")
    image = functional_ocr_fixture_image("generated-product-table")
    try:
        layouts = detect_table_layouts(
            image,
            min_confirmed_cell_ratio=0.35,
        )
    finally:
        image.close()

    assert len(layouts) == 1
    assert layouts[0].rows == 3
    assert layouts[0].cols == 4


def test_erase_table_lines_preserves_generated_table_text_strokes():
    pytest.importorskip("cv2")
    image = functional_ocr_fixture_image("generated-product-table")
    cleaned = erase_table_lines_for_ocr(image)
    try:
        original_gray = np.asarray(image.convert("L"))
        cleaned_gray = np.asarray(cleaned.convert("L"))
        original_text_ink = original_gray[250:350, 80:350] < 180
        cleaned_text_ink = cleaned_gray[250:350, 80:350] < 180

        assert np.count_nonzero(original_text_ink) > 100
        assert np.count_nonzero(cleaned_text_ink) >= np.count_nonzero(original_text_ink) * 0.75
        assert np.mean(cleaned_gray[90:500, 58:64]) > 245
    finally:
        if cleaned is not image:
            cleaned.close()
        image.close()


def test_detect_table_layouts_ignores_blank_page():
    pytest.importorskip("cv2")
    assert detect_table_layouts(Image.new("RGB", (500, 300), "white")) == []


def test_detect_table_layouts_rejects_bar_chart_as_grid():
    pytest.importorskip("cv2")
    assert (
        detect_table_layouts(
            _bar_chart_fixture(),
            min_confirmed_cell_ratio=0.35,
        )
        == []
    )


def test_convert_service_uses_table_layout_before_vertical_chunks(monkeypatch, tmp_path):
    pytest.importorskip("cv2")
    values = iter(["Предмет", "Часы", "Math", "42"])

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            return next(values)

        def info(self):
            return {"engine": "fake"}

    monkeypatch.setattr(convert_service, "AutoEngine", lambda **_kwargs: FakeEngine())

    image_path = tmp_path / "table.png"
    _table_fixture().save(image_path)

    markdown, meta = asyncio.run(
        convert_service.convert(
            image_path,
            engine_type="auto",
            pipeline_profile=OcrPipelineProfile(
                name="test",
                layout=LayoutPipelineConfig(allowed_stages=("table_regions",)),
            ),
        )
    )

    assert "| Предмет | Часы |" in markdown
    assert "| Math | 42 |" in markdown
    assert meta["tables_found"] == 1
    assert meta["table_cells"] == 4


def test_convert_service_segments_implausibly_tall_table_region(monkeypatch, tmp_path):
    recognized_sizes = []

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            raise AssertionError("tall pseudo-table reached table OCR")

        def recognize(self, image, mode="text_mode", psm=6):
            recognized_sizes.append(image.size)
            return f"chunk {len(recognized_sizes)}"

        def info(self):
            return {"engine": "fake"}

    def fake_layout(image, min_confirmed_cell_ratio=0.0):
        assert min_confirmed_cell_ratio == 0.42
        layout = _dense_table_layout(image.width, image.height, rows=21, cols=25)
        return [LayoutRegion(kind="table", image=image, bbox=layout.bbox, table=layout)]

    monkeypatch.setattr(convert_service, "AutoEngine", lambda **_kwargs: FakeEngine())
    monkeypatch.setattr(convert_service, "analyze_document_layout", fake_layout)

    image_path = tmp_path / "long-screenshot.png"
    Image.new("RGB", (800, 4200), (230, 230, 230)).save(image_path)
    profile = OcrPipelineProfile(
        name="test",
        layout=LayoutPipelineConfig(allowed_stages=("table_regions",)),
        grid_min_confirmed_cell_ratio=0.42,
    )

    markdown, meta = asyncio.run(
        convert_service.convert(
            image_path,
            engine_type="auto",
            pipeline_profile=profile,
        )
    )

    assert "chunk 1" in markdown
    assert len(recognized_sizes) > 1
    assert max(height for _, height in recognized_sizes) <= 1200
    assert meta["chunks"] == len(recognized_sizes)
    assert meta["tables_found"] == 0
    assert meta["table_cells"] == 0


def test_convert_service_bounds_large_table_fallback(monkeypatch, tmp_path):
    word_calls = []
    recognized_sizes = []

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            word_calls.append(image.size)
            return []

        def recognize(self, image, mode="text_mode", psm=6):
            recognized_sizes.append(image.size)
            return "fallback text"

        def info(self):
            return {"engine": "fake"}

    def fake_layout(image, min_confirmed_cell_ratio=0.0):
        assert min_confirmed_cell_ratio == 0.0
        layout = _dense_table_layout(image.width, image.height, rows=21, cols=25)
        return [LayoutRegion(kind="table", image=image, bbox=layout.bbox, table=layout)]

    monkeypatch.setattr(convert_service, "AutoEngine", lambda **_kwargs: FakeEngine())
    monkeypatch.setattr(convert_service, "analyze_document_layout", fake_layout)

    image_path = tmp_path / "large-table.png"
    Image.new("RGB", (800, 1000), (200, 200, 200)).save(image_path)
    profile = OcrPipelineProfile(
        name="test",
        layout=LayoutPipelineConfig(allowed_stages=("table_regions",)),
    )

    markdown, meta = asyncio.run(
        convert_service.convert(
            image_path,
            engine_type="auto",
            pipeline_profile=profile,
        )
    )

    assert "fallback text" in markdown
    assert len(word_calls) == 1
    assert recognized_sizes == [(800, 1000)]
    assert meta["chunks"] == 2
    assert meta["tables_found"] == 1
    assert meta["table_cells"] == 525


def test_convert_service_rejects_sparse_table_markdown(monkeypatch, tmp_path):
    recognized_sizes = []

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            return [{"text": "lonely", "bbox": (20, 20, 80, 50)}]

        def recognize(self, image, mode="text_mode", psm=6):
            recognized_sizes.append(image.size)
            return "raw ranking with names and numbers"

        def info(self):
            return {"engine": "fake"}

    def fake_layout(image, min_confirmed_cell_ratio=0.0):
        layout = _dense_table_layout(image.width, image.height, rows=12, cols=12)
        return [
            LayoutRegion(
                kind="table",
                image=image,
                bbox=layout.bbox,
                table=layout,
            )
        ]

    monkeypatch.setattr(
        convert_service,
        "AutoEngine",
        lambda **_kwargs: FakeEngine(),
    )
    monkeypatch.setattr(convert_service, "analyze_document_layout", fake_layout)

    image_path = tmp_path / "sparse-pseudo-table.png"
    Image.new("RGB", (900, 700), (200, 200, 200)).save(image_path)
    profile = OcrPipelineProfile(
        name="test",
        layout=LayoutPipelineConfig(allowed_stages=("table_regions",)),
        table_min_word_cell_coverage=0.35,
        max_table_cell_ocr_calls=16,
    )

    markdown, meta = asyncio.run(
        convert_service.convert(
            image_path,
            engine_type="auto",
            pipeline_profile=profile,
        )
    )

    assert markdown == "raw ranking with names and numbers"
    assert recognized_sizes == [(900, 700)]
    assert meta["chunks"] == 2
    assert meta["tables_found"] == 1
    assert meta["table_cells"] == 144


def test_convert_service_rejects_sparse_cell_markdown(monkeypatch, tmp_path):
    cell_calls = 0
    raw_calls = 0

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            return []

        def recognize(self, image, mode="text_mode", psm=6):
            nonlocal cell_calls, raw_calls
            if image.size == (400, 400):
                raw_calls += 1
                return "raw names 1 2 3 4"
            cell_calls += 1
            return "only one cell" if cell_calls == 1 else ""

        def info(self):
            return {"engine": "fake"}

    def fake_layout(image, min_confirmed_cell_ratio=0.0):
        layout = _dense_table_layout(image.width, image.height, rows=4, cols=4)
        return [
            LayoutRegion(
                kind="table",
                image=image,
                bbox=layout.bbox,
                table=layout,
            )
        ]

    monkeypatch.setattr(
        convert_service,
        "AutoEngine",
        lambda **_kwargs: FakeEngine(),
    )
    monkeypatch.setattr(convert_service, "analyze_document_layout", fake_layout)

    image_path = tmp_path / "sparse-small-table.png"
    Image.new("RGB", (400, 400), (200, 200, 200)).save(image_path)
    profile = OcrPipelineProfile(
        name="test",
        layout=LayoutPipelineConfig(allowed_stages=("table_regions",)),
        max_table_cell_ocr_calls=16,
        table_min_cell_coverage=0.5,
    )

    markdown, meta = asyncio.run(
        convert_service.convert(
            image_path,
            engine_type="auto",
            pipeline_profile=profile,
        )
    )

    assert markdown == "raw names 1 2 3 4"
    assert "only one cell" not in markdown
    assert cell_calls == 0
    assert raw_calls == 1
    assert meta["chunks"] >= 1


def test_convert_service_checks_wide_table_coverage_before_formatting(
    monkeypatch,
    tmp_path,
):
    formatted = False

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            return [{"text": "Б1.О.01", "bbox": (5, 5, 20, 20)}]

        def recognize(self, image, mode="text_mode", psm=6):
            return "raw curriculum text"

        def info(self):
            return {"engine": "fake"}

    image_path = tmp_path / "sparse-wide-table.png"
    Image.new("RGB", (1000, 200), (200, 200, 200)).save(image_path)
    layout = _dense_table_layout(1000, 200, rows=2, cols=50)

    monkeypatch.setattr(
        convert_service,
        "AutoEngine",
        lambda **_kwargs: FakeEngine(),
    )
    monkeypatch.setattr(
        convert_service,
        "analyze_document_layout",
        lambda image, min_confirmed_cell_ratio=0.0: [
            LayoutRegion(
                kind="table",
                image=image,
                bbox=layout.bbox,
                table=layout,
            )
        ],
    )

    def fake_wide_formatter(table, words):
        nonlocal formatted
        formatted = True
        return "| sparse |"

    original_formatter = convert_service.format_table_words

    def fake_formatter(name, table, words):
        if name == "curriculum":
            return fake_wide_formatter(table, words)
        return original_formatter(name, table, words)

    monkeypatch.setattr(
        convert_service,
        "format_table_words",
        fake_formatter,
    )

    profile = OcrPipelineProfile(
        name="test",
        layout=LayoutPipelineConfig(allowed_stages=("table_regions",)),
        wide_table_min_word_cell_coverage=0.02,
        max_table_cell_ocr_calls=16,
        table_layout_normalization="preserve_grid",
        table_word_recognition="single_pass_with_left_strip",
        table_word_formatters=("curriculum", "generic_markdown"),
    )
    markdown, _ = asyncio.run(
        convert_service.convert(
            image_path,
            engine_type="auto",
            pipeline_profile=profile,
        )
    )

    assert markdown == "raw curriculum text"
    assert formatted is False


def test_generic_profile_does_not_infer_curriculum_from_column_count(monkeypatch):
    layout = _dense_table_layout(1000, 200, rows=2, cols=50)
    calls = []

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            return [
                {
                    "text": "value",
                    "bbox": (5, 5, 20, 20),
                }
            ]

        def recognize(self, image, mode="text_mode", psm=6):
            return "raw table"

    region = LayoutRegion(
        kind="table",
        image=Image.new("RGB", (1000, 200), "white"),
        bbox=layout.bbox,
        table=layout,
    )
    original_formatter = convert_service.format_table_words

    def recording_formatter(name, table, words):
        calls.append(name)
        return original_formatter(name, table, words)

    monkeypatch.setattr(
        convert_service,
        "format_table_words",
        recording_formatter,
    )
    try:
        convert_service._convert_layout_region(
            region,
            FakeEngine(),
            OcrPipelineProfile(
                name="generic",
                table_min_word_cell_coverage=0,
            ),
        )
    finally:
        region.image.close()

    assert calls == ["generic_markdown"]
