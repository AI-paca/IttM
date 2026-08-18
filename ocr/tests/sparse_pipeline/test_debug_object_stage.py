from __future__ import annotations

import hashlib
import importlib.util
import pickle
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
STAGE_PATH = REPOSITORY_ROOT / "scripts" / "debug" / "debug_object_stage.py"


@pytest.fixture(scope="module")
def stage() -> ModuleType:
    module_name = "debug_object_stage_regression"
    spec = importlib.util.spec_from_file_location(module_name, STAGE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _matrix_payload(
    row_source_ids: tuple[tuple[str, str, str], ...],
) -> tuple[dict[str, object], dict[str, tuple[int, int, int, int]]]:
    boxes: dict[str, tuple[int, int, int, int]] = {}
    rows = []
    for row_index, source_ids in enumerate(row_source_ids):
        segments = []
        for column_index, source_id in enumerate(source_ids):
            left = column_index * 100
            boxes[source_id] = (
                left,
                row_index * 30,
                left + 60,
                row_index * 30 + 20,
            )
            segments.append({"source_segment_ids": [source_id]})
        rows.append({"segments": segments})
    return (
        {
            "matrix_bbox_in_crop": [0, 0, 300, 200],
            "rows": rows,
        },
        boxes,
    )


def test_reused_flowchart_segments_do_not_fake_independent_table_rows(
    stage: ModuleType,
) -> None:
    repeated_nodes = ("left-node", "center-node", "right-node")
    payload, boxes = _matrix_payload((repeated_nodes,) * 6)

    evidence = stage._matrix_table_evidence(payload, boxes)

    assert evidence["repetitions"] == 6
    assert evidence["independent_repetitions"] == 1
    assert evidence["detected"] is False


@pytest.mark.parametrize("shared_first_column", [False, True])
def test_independent_table_rows_still_trigger_validator(
    stage: ModuleType,
    shared_first_column: bool,
) -> None:
    rows = tuple(
        (
            "merged-left" if shared_first_column else f"left-{row}",
            f"center-{row}",
            f"right-{row}",
        )
        for row in range(6)
    )
    payload, boxes = _matrix_payload(rows)

    evidence = stage._matrix_table_evidence(payload, boxes)

    assert evidence["repetitions"] == 6
    assert evidence["independent_repetitions"] == 6
    assert evidence["detected"] is True


def test_ocr_handoff_state_is_versioned_and_reads_legacy_tuple(
    stage: ModuleType,
    tmp_path: Path,
) -> None:
    current = tmp_path / "current"
    value = ("stored", "block-ocr")
    stage._write_state(
        current,
        value,
        handoff_kind="ocr-blocks/get-segment",
    )

    persisted = pickle.loads((current / "state.pkl").read_bytes())
    assert persisted == {
        "schema": "ittm.debug-object-stage-state",
        "version": 1,
        "kind": "ocr-blocks/get-segment",
        "payload": value,
    }
    assert stage._read_state(current) == value

    legacy = tmp_path / "legacy"
    stage._write_state(legacy, value)
    assert stage._read_state(legacy) == value


def test_legacy_two_field_table_recovery_reconstructs_rows(
    stage: ModuleType,
) -> None:
    segment_lines = (
        "table-row-000000\tMetric\tValue",
        "table-row-000001\tBrain %\t2%",
    )
    result_text = (
        "| Metric | Value |\n"
        "| --- | --- |\n"
        "| Brain % | 2% |\n"
    )

    normalized = stage._normalize_table_recovery(
        (segment_lines, result_text)
    )

    assert normalized == (
        segment_lines,
        result_text,
        (("Metric", "Value"), ("Brain %", "2%")),
    )


def test_table_markdown_formats_only_complete_numeric_ratio_cells(
    stage: ModuleType,
) -> None:
    rows = (
        ("Kind", "Value"),
        ("Ratio", "7/8"),
        ("Path", "docs/7/8"),
        ("Date", "2026/07/31"),
        ("Percent", "7/8%"),
    )

    rendered = stage._render_table_rows(rows)

    assert rendered is not None
    segment_lines, markdown = rendered
    assert segment_lines[1] == "table-row-000001\tRatio\t7/8"
    assert "| Ratio | 7 / 8 |" in markdown
    assert "| Path | docs/7/8 |" in markdown
    assert "| Date | 2026/07/31 |" in markdown
    assert "| Percent | 7/8% |" in markdown


def test_sparse_table_coordinates_project_to_logical_rows_with_evidence(
    stage: ModuleType,
) -> None:
    cells = tuple(
        SimpleNamespace(row=row, column=column, segment_id=segment_id)
        for row, column, segment_id in (
            (0, 1, "segment-0"),
            (0, 1, "segment-1"),
            (3, 1, "segment-2"),
            (6, 0, "segment-3"),
            (9, 1, "segment-4"),
            (12, 1, "segment-5"),
            (12, 1, "segment-6"),
        )
    )
    spans = tuple(
        SimpleNamespace(
            segment_id=cell.segment_id,
            row_start=cell.row,
            row_stop=cell.row + 1,
            column_start=cell.column,
            column_stop=cell.column + 1,
        )
        for cell in cells
    )
    stored = SimpleNamespace(
        source_object_id="object-table",
        matrix=SimpleNamespace(cells=cells, spans=spans),
    )
    rows = (
        ("Metric", "Value"),
        ("Brain %", "2%"),
        ("You / Me", "7 / 8"),
        ("Concept %", "96%"),
        ("English %", "80%"),
    )

    projected = stage._table_handoff_segments(
        stored,
        rows,
        ocr_job_ids=("ocr-job-0",),
    )

    assert len(projected) == 10
    assert {
        segment_id
        for segment in projected
        for segment_id in segment["source_segment_ids"]
    } == {f"segment-{index}" for index in range(7)}
    assert {
        (segment["topology"]["row"], segment["topology"]["column"])
        for segment in projected
    } == {(row, column) for row in range(5) for column in range(2)}
    assert all(segment["segment_id"] for segment in projected)
    assert all(
        segment["source_segment_ids"]
        or segment["evidence"]["ocr_job_ids"] == ("ocr-job-0",)
        for segment in projected
    )


def test_sparse_table_handoff_keeps_empty_rows_and_cell_provenance(
    stage: ModuleType,
) -> None:
    cells = tuple(
        SimpleNamespace(row=row, column=column, segment_id=segment_id)
        for row, column, segment_id in (
            (0, 0, "segment-header"),
            (0, 1, "segment-value"),
            (1, 0, "segment-empty-source"),
            (1, 1, "segment-noise-source"),
            (1, 1, "segment-empty-value"),
            (2, 0, "segment-body"),
        )
    )
    spans = tuple(
        SimpleNamespace(
            segment_id=cell.segment_id,
            row_start=cell.row,
            row_stop=(
                cell.row + 2
                if cell.segment_id == "segment-noise-source"
                else cell.row + 1
            ),
            column_start=cell.column,
            column_stop=cell.column + 1,
        )
        for cell in cells
    )
    stored = SimpleNamespace(
        source_object_id="object-sparse-table",
        matrix=SimpleNamespace(cells=cells, spans=spans),
    )

    projected = stage._table_handoff_segments(
        stored,
        (
            ("Metric", "Value"),
            ("", ""),
            ("Recognized", ""),
        ),
        ocr_job_ids=("ocr-job-0",),
    )

    by_coordinate = {
        (
            segment["topology"]["row"],
            segment["topology"]["column"],
        ): segment
        for segment in projected
    }
    assert set(by_coordinate) == {
        (row, column) for row in range(3) for column in range(2)
    }
    assert by_coordinate[(1, 0)]["text"] == ""
    assert by_coordinate[(1, 0)]["source_segment_ids"] == (
        "segment-empty-source",
    )
    assert by_coordinate[(1, 1)]["text"] == ""
    assert by_coordinate[(1, 1)]["source_segment_ids"] == (
        "segment-empty-value",
        "segment-noise-source",
    )
    assert by_coordinate[(2, 1)]["text"] == ""
    assert by_coordinate[(2, 1)]["source_segment_ids"] == ()
    projected_source_ids = {
        segment_id
        for segment in projected
        for segment_id in segment["source_segment_ids"]
    }
    assert len({segment["segment_id"] for segment in projected}) == len(
        projected
    )
    assert projected_source_ids == {
        cell.segment_id for cell in cells
    }
    assert "segment-actually-missing" not in projected_source_ids


class _ArtifactEnhancer:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0

    def enhance_many(self, _crops: object) -> tuple[SimpleNamespace, ...]:
        self.calls += 1
        return (SimpleNamespace(png_bytes=self.payload),)


@pytest.mark.parametrize(
    ("transform_name", "expected_source", "expected_payload"),
    (
        ("RAW", "raw", b"raw-png"),
        ("GAMMA", "stored-gamma", b"stored-gamma-png"),
        (
            "CONTEXTUAL_COMPOSITE",
            "contextual-composite",
            b"raw-png",
        ),
    ),
)
def test_ocr_input_artifact_binds_saved_transform_payloads(
    stage: ModuleType,
    transform_name: str,
    expected_source: str,
    expected_payload: bytes,
) -> None:
    raw = b"raw-png"
    gamma = b"stored-gamma-png"
    enhancer = _ArtifactEnhancer(b"recomputed-gamma-png")
    job = SimpleNamespace(
        block_id="block-1",
        transform=getattr(stage.OcrTransform, transform_name),
        input_sha256=hashlib.sha256(expected_payload).hexdigest(),
        context_sha256=hashlib.sha256(raw).hexdigest(),
    )
    crop = SimpleNamespace(
        raw=SimpleNamespace(png_bytes=raw),
        gamma=SimpleNamespace(png_bytes=gamma),
    )

    artifact = stage._ocr_input_artifact(
        job,
        crop,
        enhancer=enhancer,
        enhanced_by_block={},
    )

    assert artifact.png_bytes == expected_payload
    assert artifact.source == expected_source
    assert artifact.sha256 == job.input_sha256
    assert artifact.digest_matches is True
    assert artifact.context_sha256 == job.context_sha256
    assert artifact.context_digest_matches is True
    assert enhancer.calls == 0


def test_ocr_input_artifact_recovers_exact_gamma_payload_by_digest(
    stage: ModuleType,
) -> None:
    raw = b"raw-png"
    actual_gamma = b"actual-gamma-png"
    enhancer = _ArtifactEnhancer(actual_gamma)
    job = SimpleNamespace(
        block_id="block-1",
        transform=stage.OcrTransform.GAMMA,
        input_sha256=hashlib.sha256(actual_gamma).hexdigest(),
        context_sha256=hashlib.sha256(raw).hexdigest(),
    )
    crop = SimpleNamespace(
        raw=SimpleNamespace(png_bytes=raw),
        gamma=SimpleNamespace(png_bytes=b"stale-gamma-png"),
    )

    artifact = stage._ocr_input_artifact(
        job,
        crop,
        enhancer=enhancer,
        enhanced_by_block={},
    )

    assert artifact.png_bytes == actual_gamma
    assert artifact.source == "recomputed-gamma"
    assert artifact.digest_matches is True
    assert artifact.context_digest_matches is True
    assert enhancer.calls == 1


def test_ocr_input_artifact_never_false_passes_forged_digests(
    stage: ModuleType,
) -> None:
    enhancer = _ArtifactEnhancer(b"recomputed-gamma-png")
    job = SimpleNamespace(
        block_id="block-1",
        transform=stage.OcrTransform.GAMMA,
        input_sha256="f" * 64,
        context_sha256="e" * 64,
    )
    crop = SimpleNamespace(
        raw=SimpleNamespace(png_bytes=b"raw-png"),
        gamma=SimpleNamespace(png_bytes=b"stored-gamma-png"),
    )

    artifact = stage._ocr_input_artifact(
        job,
        crop,
        enhancer=enhancer,
        enhanced_by_block={},
    )

    assert artifact.source == "stored-gamma"
    assert artifact.digest_matches is False
    assert artifact.context_digest_matches is False
    assert artifact.sha256 != job.input_sha256
    assert artifact.context_sha256 != job.context_sha256
    assert enhancer.calls == 1


def test_source_placement_fallback_uses_captured_digest(
    stage: ModuleType,
) -> None:
    raw = b"canonical-raw"
    source = b"source-placement-line"
    job = SimpleNamespace(
        block_id="block-1",
        transform=stage.OcrTransform.SOURCE_PLACEMENT_FALLBACK,
        input_sha256=hashlib.sha256(source).hexdigest(),
        context_sha256=hashlib.sha256(raw).hexdigest(),
    )
    crop = SimpleNamespace(
        raw=SimpleNamespace(png_bytes=raw),
        gamma=None,
    )

    artifact = stage._ocr_input_artifact(
        job,
        crop,
        enhancer=_ArtifactEnhancer(b"unused"),
        enhanced_by_block={},
        selected_attempt_input=source,
    )

    assert artifact.png_bytes == source
    assert artifact.source == "source-placement-fallback"
    assert artifact.digest_matches is True
    assert artifact.context_digest_matches is True


def test_node_package_root_prefers_local_then_git_common_worktree(
    stage: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked = tmp_path / "linked"
    common_root = tmp_path / "main"
    local_package = linked / "node_modules" / "tsx" / "package.json"
    common_package = common_root / "node_modules" / "tsx" / "package.json"
    local_package.parent.mkdir(parents=True)
    local_package.write_text("{}", encoding="utf-8")

    def unexpected_git(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("local package lookup must not invoke git")

    monkeypatch.setattr(stage.subprocess, "run", unexpected_git)
    assert stage._node_package_root("tsx", repository_root=linked) == linked

    local_package.unlink()
    common_package.parent.mkdir(parents=True)
    common_package.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        stage.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=str(common_root / ".git") + "\n",
            stderr="",
        ),
    )
    assert stage._node_package_root("tsx", repository_root=linked) == common_root


def test_node_package_root_fails_clearly_when_package_is_missing(
    stage: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked = tmp_path / "linked"
    linked.mkdir()
    monkeypatch.setattr(
        stage.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=str(tmp_path / "main" / ".git") + "\n",
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="required Node package 'tsx'"):
        stage._node_package_root("tsx", repository_root=linked)
