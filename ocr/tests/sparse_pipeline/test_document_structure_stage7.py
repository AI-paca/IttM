from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Any

import pytest

from app.sparse_pipeline.contracts import Box, GeometryResult, RuleAxis
import app.sparse_pipeline.document_assembly as document_assembly
from app.sparse_pipeline.object_reconstruction import ObjectKind
from app.sparse_pipeline.ocr_fusion import compact_ocr_text

from tests.sparse_pipeline.test_document_assembly_stage7 import (
    _assemble,
    _fixture,
    _geometry,
    _list_geometry,
    _matrix,
    _paragraph_geometry,
    _rule,
    _segment,
    _word_boxes,
)


def _structural_kind(name: str) -> Any:
    enum_type = getattr(document_assembly, "StructuralUnitKind", None)
    assert enum_type is not None, (
        "Stage 7 must expose StructuralUnitKind independently of ObjectKind"
    )
    return getattr(enum_type, name)


def _assemble_words(
    geometry: GeometryResult,
    texts: dict[str, str],
) -> object:
    return _assemble(_fixture(geometry, _word_boxes(geometry, texts)))


def _assert_canonical_unit_ids(result: object) -> None:
    units = getattr(result, "structural_units")
    assert tuple(unit.unit_id for unit in units) == tuple(
        f"unit-{index:08d}" for index in range(len(units))
    )


def _assert_payload_once_in_order(
    value: str,
    expected: tuple[str, ...],
) -> None:
    compact = compact_ocr_text(value)
    assert compact == "".join(compact_ocr_text(item) for item in expected)
    counts = Counter(compact)
    assert counts == Counter(
        character
        for item in expected
        for character in compact_ocr_text(item)
    )


def _sparse_table_with_empty_cell() -> GeometryResult:
    width, height = 210, 46
    segments = (
        _segment("e-00", Box(4, 4, 56, 20), row_index=0, component_id=0),
        _segment("e-01", Box(64, 4, 126, 20), row_index=0, component_id=1),
        _segment("e-02", Box(134, 4, 204, 20), row_index=0, component_id=2),
        _segment("e-10", Box(4, 26, 56, 42), row_index=1, component_id=3),
        _segment("e-12", Box(134, 26, 204, 42), row_index=1, component_id=4),
    )
    rules = (
        _rule("e-h0", Box(0, 0, width, 2), axis=RuleAxis.HORIZONTAL),
        _rule("e-h1", Box(0, 22, width, 24), axis=RuleAxis.HORIZONTAL),
        _rule("e-h2", Box(0, 44, width, 46), axis=RuleAxis.HORIZONTAL),
        _rule("e-v0", Box(0, 0, 2, height), axis=RuleAxis.VERTICAL),
        _rule("e-v1", Box(60, 0, 62, height), axis=RuleAxis.VERTICAL),
        _rule("e-v2", Box(130, 0, 132, height), axis=RuleAxis.VERTICAL),
        _rule("e-v3", Box(208, 0, 210, height), axis=RuleAxis.VERTICAL),
    )
    return _geometry(
        aligned_size=(width, height),
        segments=segments,
        rules=rules,
        matrix=_matrix(
            row_edges=(0, 2, 22, 24, 44, 46),
            column_edges=(0, 2, 60, 62, 130, 132, 208, 210),
            placements={
                "e-00": ((1, 1),),
                "e-01": ((1, 3),),
                "e-02": ((1, 5),),
                "e-10": ((3, 1),),
                "e-12": ((3, 5),),
            },
            horizontal_rule_rows=(0, 2, 4),
            vertical_rule_columns=(0, 2, 4, 6),
        ),
    )


def _sparse_table_with_spanning_cell() -> GeometryResult:
    width, height = 210, 46
    segments = (
        _segment(
            "s-top-wide",
            Box(4, 4, 126, 20),
            row_index=0,
            component_id=0,
        ),
        _segment("s-top-2", Box(134, 4, 204, 20), row_index=0, component_id=1),
        _segment("s-10", Box(4, 26, 56, 42), row_index=1, component_id=2),
        _segment("s-11", Box(64, 26, 126, 42), row_index=1, component_id=3),
        _segment("s-12", Box(134, 26, 204, 42), row_index=1, component_id=4),
    )
    rules = (
        _rule("s-h0", Box(0, 0, width, 2), axis=RuleAxis.HORIZONTAL),
        _rule("s-h1", Box(0, 22, width, 24), axis=RuleAxis.HORIZONTAL),
        _rule("s-h2", Box(0, 44, width, 46), axis=RuleAxis.HORIZONTAL),
        _rule("s-v0", Box(0, 0, 2, height), axis=RuleAxis.VERTICAL),
        _rule("s-v1", Box(60, 22, 62, height), axis=RuleAxis.VERTICAL),
        _rule("s-v2", Box(130, 0, 132, height), axis=RuleAxis.VERTICAL),
        _rule("s-v3", Box(208, 0, 210, height), axis=RuleAxis.VERTICAL),
    )
    return _geometry(
        aligned_size=(width, height),
        segments=segments,
        rules=rules,
        matrix=_matrix(
            row_edges=(0, 2, 22, 24, 44, 46),
            column_edges=(0, 2, 60, 62, 130, 132, 208, 210),
            placements={
                "s-top-wide": ((1, 1), (1, 2), (1, 3)),
                "s-top-2": ((1, 5),),
                "s-10": ((3, 1),),
                "s-11": ((3, 3),),
                "s-12": ((3, 5),),
            },
            horizontal_rule_rows=(0, 2, 4),
            vertical_rule_columns=(0, 2, 4, 6),
        ),
    )


def _table_with_fully_covered_rowspan_row() -> GeometryResult:
    width, height = 122, 68
    segments = (
        _segment(
            "r-top-left",
            Box(4, 4, 56, 42),
            row_index=0,
            component_id=0,
        ),
        _segment(
            "r-top-right",
            Box(64, 4, 118, 42),
            row_index=0,
            component_id=1,
        ),
        _segment(
            "r-bottom-left",
            Box(4, 48, 56, 64),
            row_index=2,
            component_id=2,
        ),
        _segment(
            "r-bottom-right",
            Box(64, 48, 118, 64),
            row_index=2,
            component_id=3,
        ),
    )
    rules = (
        _rule("r-h0", Box(0, 0, width, 2), axis=RuleAxis.HORIZONTAL),
        _rule("r-h1", Box(0, 22, 2, 24), axis=RuleAxis.HORIZONTAL),
        _rule("r-h2", Box(0, 44, width, 46), axis=RuleAxis.HORIZONTAL),
        _rule("r-h3", Box(0, 66, width, 68), axis=RuleAxis.HORIZONTAL),
        _rule("r-v0", Box(0, 0, 2, height), axis=RuleAxis.VERTICAL),
        _rule("r-v1", Box(60, 0, 62, height), axis=RuleAxis.VERTICAL),
        _rule("r-v2", Box(120, 0, 122, height), axis=RuleAxis.VERTICAL),
    )
    return _geometry(
        aligned_size=(width, height),
        segments=segments,
        rules=rules,
        matrix=_matrix(
            row_edges=(0, 2, 22, 24, 44, 46, 66, 68),
            column_edges=(0, 2, 60, 62, 120, 122),
            placements={
                "r-top-left": ((1, 1), (2, 1), (3, 1)),
                "r-top-right": ((1, 3), (2, 3), (3, 3)),
                "r-bottom-left": ((5, 1),),
                "r-bottom-right": ((5, 3),),
            },
            horizontal_rule_rows=(0, 2, 4, 6),
            vertical_rule_columns=(0, 2, 4),
        ),
    )


def _table_with_fully_covered_colspan_column() -> GeometryResult:
    width, height = 210, 46
    segments = (
        _segment(
            "c-top-wide",
            Box(4, 4, 126, 20),
            row_index=0,
            component_id=0,
        ),
        _segment(
            "c-top-right",
            Box(134, 4, 204, 20),
            row_index=0,
            component_id=1,
        ),
        _segment(
            "c-bottom-wide",
            Box(4, 26, 126, 42),
            row_index=1,
            component_id=2,
        ),
        _segment(
            "c-bottom-right",
            Box(134, 26, 204, 42),
            row_index=1,
            component_id=3,
        ),
    )
    rules = (
        _rule("c-h0", Box(0, 0, width, 2), axis=RuleAxis.HORIZONTAL),
        _rule("c-h1", Box(0, 22, width, 24), axis=RuleAxis.HORIZONTAL),
        _rule("c-h2", Box(0, 44, width, 46), axis=RuleAxis.HORIZONTAL),
        _rule("c-v0", Box(0, 0, 2, height), axis=RuleAxis.VERTICAL),
        _rule("c-v1", Box(60, 0, 62, 2), axis=RuleAxis.VERTICAL),
        _rule("c-v2", Box(130, 0, 132, height), axis=RuleAxis.VERTICAL),
        _rule("c-v3", Box(208, 0, 210, height), axis=RuleAxis.VERTICAL),
    )
    return _geometry(
        aligned_size=(width, height),
        segments=segments,
        rules=rules,
        matrix=_matrix(
            row_edges=(0, 2, 22, 24, 44, 46),
            column_edges=(0, 2, 60, 62, 130, 132, 208, 210),
            placements={
                "c-top-wide": ((1, 1), (1, 2), (1, 3)),
                "c-top-right": ((1, 5),),
                "c-bottom-wide": ((3, 1), (3, 2), (3, 3)),
                "c-bottom-right": ((3, 5),),
            },
            horizontal_rule_rows=(0, 2, 4),
            vertical_rule_columns=(0, 2, 4, 6),
        ),
    )


def _table_with_empty_outer_rows_and_columns() -> GeometryResult:
    width = height = 82
    segments = (
        _segment("o-11", Box(24, 24, 38, 38), row_index=1, component_id=0),
        _segment("o-12", Box(44, 24, 58, 38), row_index=1, component_id=1),
        _segment("o-21", Box(24, 44, 38, 58), row_index=2, component_id=2),
        _segment("o-22", Box(44, 44, 58, 58), row_index=2, component_id=3),
    )
    horizontal = tuple(
        _rule(
            f"o-h{index}",
            Box(0, offset, width, offset + 2),
            axis=RuleAxis.HORIZONTAL,
        )
        for index, offset in enumerate((0, 20, 40, 60, 80))
    )
    vertical = tuple(
        _rule(
            f"o-v{index}",
            Box(offset, 0, offset + 2, height),
            axis=RuleAxis.VERTICAL,
        )
        for index, offset in enumerate((0, 20, 40, 60, 80))
    )
    return _geometry(
        aligned_size=(width, height),
        segments=segments,
        rules=horizontal + vertical,
        matrix=_matrix(
            row_edges=(0, 2, 20, 22, 40, 42, 60, 62, 80, 82),
            column_edges=(0, 2, 20, 22, 40, 42, 60, 62, 80, 82),
            placements={
                "o-11": ((3, 3),),
                "o-12": ((3, 5),),
                "o-21": ((5, 3),),
                "o-22": ((5, 5),),
            },
            horizontal_rule_rows=(0, 2, 4, 6, 8),
            vertical_rule_columns=(0, 2, 4, 6, 8),
        ),
    )


def _two_consecutive_tables() -> GeometryResult:
    width, height = 166, 106
    segments = tuple(
        _segment(
            f"{prefix}-{row}{column}",
            Box(
                4 if column == 0 else 86,
                top + 4 + row * 22,
                78 if column == 0 else 160,
                top + 20 + row * 22,
            ),
            row_index=row_offset + row,
            component_id=component_offset + row * 2 + column,
        )
        for prefix, top, row_offset, component_offset in (
            ("a", 0, 0, 0),
            ("b", 60, 2, 4),
        )
        for row in range(2)
        for column in range(2)
    )
    rules = tuple(
        rule
        for prefix, top in (("a", 0), ("b", 60))
        for rule in (
            _rule(
                f"{prefix}-h0",
                Box(0, top, width, top + 2),
                axis=RuleAxis.HORIZONTAL,
            ),
            _rule(
                f"{prefix}-h1",
                Box(0, top + 22, width, top + 24),
                axis=RuleAxis.HORIZONTAL,
            ),
            _rule(
                f"{prefix}-h2",
                Box(0, top + 44, width, top + 46),
                axis=RuleAxis.HORIZONTAL,
            ),
            _rule(
                f"{prefix}-v0",
                Box(0, top, 2, top + 46),
                axis=RuleAxis.VERTICAL,
            ),
            _rule(
                f"{prefix}-v1",
                Box(82, top, 84, top + 46),
                axis=RuleAxis.VERTICAL,
            ),
            _rule(
                f"{prefix}-v2",
                Box(164, top, 166, top + 46),
                axis=RuleAxis.VERTICAL,
            ),
        )
    )
    return _geometry(
        aligned_size=(width, height),
        segments=segments,
        rules=rules,
        matrix=_matrix(
            row_edges=(0, 2, 22, 24, 44, 46, 60, 62, 82, 84, 104, 106),
            column_edges=(0, 2, 82, 84, 164, 166),
            placements={
                "a-00": ((1, 1),),
                "a-01": ((1, 3),),
                "a-10": ((3, 1),),
                "a-11": ((3, 3),),
                "b-00": ((7, 1),),
                "b-01": ((7, 3),),
                "b-10": ((9, 1),),
                "b-11": ((9, 3),),
            },
            horizontal_rule_rows=(0, 2, 4, 6, 8, 10),
            vertical_rule_columns=(0, 2, 4),
        ),
    )


def test_structural_unit_kind_is_a_separate_closed_contract() -> None:
    enum_type = getattr(document_assembly, "StructuralUnitKind", None)

    assert enum_type is not None
    assert tuple(item.name for item in enum_type) == (
        "PARAGRAPH_LINE",
        "LIST_ITEM",
        "TABLE_CELL",
        "UNKNOWN_FRAGMENT",
    )
    assert tuple(item.value for item in enum_type) == (
        "paragraph_line",
        "list_item",
        "table_cell",
        "unknown_fragment",
    )


def test_paragraph_has_one_canonical_unit_per_visual_row() -> None:
    texts = {"p-0": "line-α", "p-1": "строка-β"}
    result = _assemble_words(_paragraph_geometry(), texts)

    units = result.structural_units
    assert tuple(unit.unit_kind for unit in units) == (
        _structural_kind("PARAGRAPH_LINE"),
        _structural_kind("PARAGRAPH_LINE"),
    )
    assert tuple(unit.segment_ids for unit in units) == (("p-0",), ("p-1",))
    assert tuple(
        (unit.row_start, unit.row_stop, unit.column_start, unit.column_stop)
        for unit in units
    ) == ((0, 1, 0, 1), (1, 2, 0, 1))
    assert tuple(unit.text for unit in units) == tuple(texts.values())
    _assert_canonical_unit_ids(result)
    assert result.objects[0].structural_unit_ids == tuple(
        unit.unit_id for unit in units
    )


def test_list_has_one_marker_and_body_unit_per_visual_row() -> None:
    texts = {
        "marker-0": "1.",
        "item-0": "один",
        "marker-1": "•",
        "item-1": "two",
        "marker-2": "三.",
        "item-2": "項目",
    }
    result = _assemble_words(_list_geometry(), texts)

    units = result.structural_units
    assert len(units) == 3
    assert all(
        unit.unit_kind is _structural_kind("LIST_ITEM") for unit in units
    )
    assert tuple(unit.segment_ids for unit in units) == (
        ("marker-0", "item-0"),
        ("marker-1", "item-1"),
        ("marker-2", "item-2"),
    )
    assert tuple((unit.row_start, unit.row_stop) for unit in units) == (
        (0, 1),
        (2, 3),
        (4, 5),
    )
    for index, unit in enumerate(units):
        expected = texts[f"marker-{index}"], texts[f"item-{index}"]
        _assert_payload_once_in_order(unit.text, expected)
    assert result.objects[0].structural_unit_ids == tuple(
        unit.unit_id for unit in units
    )


def test_sparse_table_materializes_empty_cells_in_row_major_order() -> None:
    geometry = _sparse_table_with_empty_cell()
    texts = {
        "e-00": "甲",
        "e-01": "Б",
        "e-02": "C",
        "e-10": "δ",
        "e-12": "五",
    }
    fixture = _fixture(geometry, _word_boxes(geometry, texts))
    assert tuple(item.kind for item in fixture.objects.objects) == (ObjectKind.TABLE,)

    result = _assemble(fixture)

    units = result.structural_units
    assert len(units) == 6
    assert all(
        unit.unit_kind is _structural_kind("TABLE_CELL") for unit in units
    )
    assert tuple((unit.row_start, unit.column_start) for unit in units) == (
        (1, 1),
        (1, 3),
        (1, 5),
        (3, 1),
        (3, 3),
        (3, 5),
    )
    empty = units[4]
    assert empty.segment_ids == ()
    assert empty.candidate_text == ""
    assert empty.text == ""
    assert empty.evidence_slice_ids == ()
    assert (
        empty.row_start,
        empty.row_stop,
        empty.column_start,
        empty.column_stop,
    ) == (3, 4, 3, 4)
    assert result.objects[0].structural_unit_ids == tuple(
        unit.unit_id for unit in units
    )
    assert result.candidate_markdown.splitlines() == [
        "| 甲 | Б | C |",
        "| δ |  | 五 |",
    ]
    assert "|  |" in result.candidate_markdown
    assert all(
        line.count("|") == 4
        for line in result.candidate_markdown.splitlines()
    )


def test_spanning_table_cell_is_one_unit_with_one_sparse_span() -> None:
    geometry = _sparse_table_with_spanning_cell()
    texts = {
        "s-top-wide": "SPAN文字",
        "s-top-2": "TOP",
        "s-10": "L",
        "s-11": "M",
        "s-12": "R",
    }
    result = _assemble_words(geometry, texts)

    assert (
        result.objects[0].table_row_indices,
        result.objects[0].table_column_indices,
    ) == ((1, 3), (1, 3, 5))

    spanning = tuple(
        unit
        for unit in result.structural_units
        if "s-top-wide" in unit.segment_ids
    )
    assert len(spanning) == 1
    unit = spanning[0]
    assert unit.unit_kind is _structural_kind("TABLE_CELL")
    assert unit.segment_ids == ("s-top-wide",)
    assert (unit.row_start, unit.row_stop) == (1, 2)
    assert (unit.column_start, unit.column_stop) == (1, 4)
    assert unit.text == texts["s-top-wide"]
    assert sum(
        candidate.text.count(texts["s-top-wide"])
        for candidate in result.structural_units
        if candidate.text is not None
    ) == 1
    assert result.candidate_markdown.splitlines() == [
        "| SPAN文字 | ::merge-left:: | TOP |",
        "| L | M | R |",
    ]
    assert result.candidate_markdown.count("::merge-left::") == 1
    assert all(line.count("|") == 4 for line in result.candidate_markdown.splitlines())


def test_rowspan_preserves_a_fully_covered_logical_row() -> None:
    geometry = _table_with_fully_covered_rowspan_row()
    result = _assemble_words(
        geometry,
        {
            "r-top-left": "TL",
            "r-top-right": "TR",
            "r-bottom-left": "BL",
            "r-bottom-right": "BR",
        },
    )

    assert (
        result.objects[0].table_row_indices,
        result.objects[0].table_column_indices,
    ) == ((1, 3, 5), (1, 3))

    assert result.candidate_markdown.splitlines() == [
        "| TL | TR |",
        "| ::merge-up:: | ::merge-up:: |",
        "| BL | BR |",
    ]


def test_colspan_preserves_a_fully_covered_logical_column() -> None:
    geometry = _table_with_fully_covered_colspan_column()
    result = _assemble_words(
        geometry,
        {
            "c-top-wide": "TW",
            "c-top-right": "TR",
            "c-bottom-wide": "BW",
            "c-bottom-right": "BR",
        },
    )

    assert (
        result.objects[0].table_row_indices,
        result.objects[0].table_column_indices,
    ) == ((1, 3), (1, 3, 5))

    assert result.candidate_markdown.splitlines() == [
        "| TW | ::merge-left:: | TR |",
        "| BW | ::merge-left:: | BR |",
    ]


def test_table_preserves_completely_empty_outer_rows_and_columns() -> None:
    geometry = _table_with_empty_outer_rows_and_columns()
    fixture = _fixture(
        geometry,
        _word_boxes(
            geometry,
            {"o-11": "A", "o-12": "B", "o-21": "C", "o-22": "D"},
        ),
    )
    assert tuple(item.kind for item in fixture.objects.objects) == (
        ObjectKind.TABLE,
    )
    document_object = fixture.objects.objects[0]
    assert (
        document_object.row_start,
        document_object.row_stop,
        document_object.column_start,
        document_object.column_stop,
    ) == (0, 9, 0, 9)

    result = _assemble(fixture)

    assembled_object = result.objects[0]
    assert assembled_object.table_row_indices == (1, 3, 5, 7)
    assert assembled_object.table_column_indices == (1, 3, 5, 7)
    assert len(result.structural_units) == 16
    assert sum(not item.segment_ids for item in result.structural_units) == 12
    assert result.candidate_markdown.splitlines() == [
        "|  |  |  |  |",
        "|  | A | B |  |",
        "|  | C | D |  |",
        "|  |  |  |  |",
    ]


@pytest.mark.parametrize(
    ("field", "forged_indices"),
    (
        ("table_row_indices", (1, 5)),
        ("table_column_indices", (1,)),
        ("table_row_indices", (1, 3, 5, 7)),
        ("table_column_indices", (1, 3, 5)),
    ),
)
def test_document_contract_rejects_forged_table_axes(
    field: str,
    forged_indices: tuple[int, ...],
) -> None:
    geometry = _table_with_fully_covered_rowspan_row()
    result = _assemble_words(
        geometry,
        {
            "r-top-left": "TL",
            "r-top-right": "TR",
            "r-bottom-left": "BL",
            "r-bottom-right": "BR",
        },
    )
    assert result.objects[0].table_row_indices == (1, 3, 5)
    assert result.objects[0].table_column_indices == (1, 3)
    forged_object = replace(
        result.objects[0],
        **{field: forged_indices},
    )

    with pytest.raises(
        (ValueError, document_assembly.DocumentAssemblyInvariantError),
        match="Markdown|logical grid",
    ):
        replace(result, objects=(forged_object,))


def test_consecutive_table_objects_keep_disjoint_ordered_unit_sets() -> None:
    geometry = _two_consecutive_tables()
    texts = {
        "a-00": "Aα",
        "a-01": "Aβ",
        "a-10": "Aγ",
        "a-11": "Aδ",
        "b-00": "B甲",
        "b-01": "B乙",
        "b-10": "B丙",
        "b-11": "B丁",
    }
    fixture = _fixture(geometry, _word_boxes(geometry, texts))
    assert tuple(item.kind for item in fixture.objects.objects) == (
        ObjectKind.TABLE,
        ObjectKind.TABLE,
    )
    assert tuple(
        (
            item.row_start,
            item.row_stop,
            item.column_start,
            item.column_stop,
        )
        for item in fixture.objects.objects
    ) == ((0, 5, 0, 5), (6, 11, 0, 5))

    first = _assemble(fixture)
    second = _assemble(fixture)

    assert first == second
    assert len(first.objects) == 2
    assert len(first.structural_units) == 8
    first_ids = first.objects[0].structural_unit_ids
    second_ids = first.objects[1].structural_unit_ids
    assert first_ids == tuple(
        unit.unit_id
        for unit in first.structural_units
        if unit.object_id == first.objects[0].object_id
    )
    assert second_ids == tuple(
        unit.unit_id
        for unit in first.structural_units
        if unit.object_id == first.objects[1].object_id
    )
    assert first_ids == tuple(f"unit-{index:08d}" for index in range(4))
    assert second_ids == tuple(f"unit-{index:08d}" for index in range(4, 8))
    assert set(first_ids).isdisjoint(second_ids)
    expected = tuple(texts.values())
    _assert_payload_once_in_order(first.candidate_text, expected)
    for payload in expected:
        assert first.candidate_markdown.count(payload) == 1
    assert first.candidate_text == second.candidate_text
    assert first.candidate_markdown == second.candidate_markdown
    assert first.candidate_markdown.split("\n\n") == [
        "| Aα | Aβ |\n| Aγ | Aδ |",
        "| B甲 | B乙 |\n| B丙 | B丁 |",
    ]


def test_single_line_unknown_object_is_an_unknown_fragment_unit() -> None:
    geometry = _geometry(
        aligned_size=(96, 24),
        segments=(
            _segment("u-0", Box(4, 4, 92, 18), row_index=0, component_id=0),
        ),
        matrix=_matrix(
            row_edges=(0, 24),
            column_edges=(0, 96),
            placements={"u-0": ((0, 0),)},
        ),
    )
    fixture = _fixture(geometry, _word_boxes(geometry, {"u-0": "heading"}))
    assert fixture.objects.objects[0].kind is ObjectKind.UNKNOWN

    result = _assemble(fixture)

    assert len(result.structural_units) == 1
    assert (
        result.structural_units[0].unit_kind
        is _structural_kind("UNKNOWN_FRAGMENT")
    )
    assert result.structural_units[0].segment_ids == ("u-0",)
    assert result.structural_units[0].text == "heading"
