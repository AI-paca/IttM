from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.sparse_pipeline.block_crops import BlockCropConfig
from app.sparse_pipeline.contracts import GeometryStatus
from app.sparse_pipeline.document_assembly import AssemblyStatus
from app.sparse_pipeline.ocr_fusion import OcrFusionStatus


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "debug"
        / "debug_document_assembly.py"
    )
    name = "_stage7_debug_document_assembly_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _prepared(runner: ModuleType, *, blocks: tuple[object, ...] = ()) -> object:
    geometry = SimpleNamespace(
        status=GeometryStatus.COMPLETE,
        segmentation=SimpleNamespace(segments=()),
    )
    return runner.PreparedItem(
        source="nested/page.png",
        run_id="page",
        preparation_seconds=0.01,
        completed_stages=(3, 1, 6, 4, 5),
        stage_seconds=((3, 0.001), (1, 0.002), (6, 0.001), (4, 0.002), (5, 0.004)),
        geometry_status="complete",
        stage4_candidate_sha256="0" * 64,
        page=object(),
        geometry=geometry,
        objects=SimpleNamespace(objects=()),
        plan=SimpleNamespace(blocks=blocks),
        crops=(),
        crop_config=BlockCropConfig(),
    )


@pytest.mark.parametrize(
    ("reference", "recognized", "expected"),
    (
        (" A\tB\n中\u00a0文 ", "AB中X", (1, 4, 4, 75.0)),
        ("Latin A", "Latin А", (1, 6, 6, 100.0 * 5 / 6)),
        ("\n\t\u2003", " ", (0, 0, 0, 100.0)),
        ("abc", "a b c d", (1, 3, 4, 100.0 * 2 / 3)),
    ),
)
def test_metric_is_exact_levenshtein_after_only_unicode_whitespace_is_removed(
    runner: ModuleType,
    reference: str,
    recognized: str,
    expected: tuple[int, int, int, float],
) -> None:
    actual = runner._metric(reference, recognized)
    assert actual[:3] == expected[:3]
    assert actual[3] == pytest.approx(expected[3])


def test_metric_scores_long_alignment_without_quadratic_matrix(
    runner: ModuleType,
) -> None:
    assert runner._metric(
        "a" * 2_000,
        "b" * 2_000,
        max_cells=4_000_000,
    ) == (2_000, 2_000, 2_000, 0.0)


@pytest.mark.parametrize("value", (-1.0, 100.1, float("nan"), float("inf")))
def test_quality_gate_rejects_invalid_accuracy_thresholds(
    runner: ModuleType, value: float
) -> None:
    with pytest.raises(ValueError, match="minimum accuracy percent"):
        runner.QualityGatePolicy(minimum_accuracy_percent=value)


def test_stage3_control_gate_completes_before_image_work(runner: ModuleType) -> None:
    runner._stage3_gate()
    assert runner.PIPELINE_ORDER == (3, 1, 6, 4, 5, 2, 7)


def test_discovery_uses_source_pngs_and_excludes_debug_masks(
    runner: ModuleType, tmp_path: Path
) -> None:
    for name in (
        "page.png",
        "page.mask.png",
        "page.line-owner.mask.png",
        "page-overlay.png",
        "aligned.png",
    ):
        (tmp_path / name).write_bytes(b"fixture")
    assert runner._discover(tmp_path) == (tmp_path / "page.png",)


def test_preparation_queue_is_bounded_and_concurrent(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = tuple(tmp_path / f"page-{index}.png" for index in range(9))
    lock = threading.Lock()
    active = 0
    peak = 0
    thread_ids: set[int] = set()

    def prepare(source: str, _root: str, _backend: str) -> object:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            thread_ids.add(threading.get_ident())
        time.sleep(0.025)
        with lock:
            active -= 1
        return runner.PreparedItem(
            source=Path(source).name,
            run_id=Path(source).stem,
            preparation_seconds=0.025,
            completed_stages=(),
            stage_seconds=(),
            error="fixture-only",
        )

    monkeypatch.setattr(runner, "_prepare_item", prepare)
    values = tuple(
        runner._prepared_stream(
            sources,
            input_root=tmp_path,
            workers=4,
            window=3,
            executor_kind="thread",
            enhancement_backend="numpy",
        )
    )
    assert len(values) == len(sources)
    assert peak == 3
    assert len(thread_ids) >= 2
    assert active == 0


def test_reference_is_loaded_only_after_stage7_artifact_publication(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    queue = SimpleNamespace(jobs=(), complete=0, failed=0)
    fusion = SimpleNamespace(status=OcrFusionStatus.COMPLETE)
    result = SimpleNamespace(
        status=AssemblyStatus.COMPLETE,
        text="A B",
        candidate_text="A B",
        markdown="| A | B |",
        candidate_markdown="| A | B |",
        evidence_slices=(),
        structural_units=(),
    )

    class Sessions:
        def run(self, **_kwargs: object) -> object:
            events.append("queue")
            return queue

    class Fusion:
        def __init__(self, _config: object) -> None:
            pass

        def fuse(self, **_kwargs: object) -> object:
            events.append("fusion")
            return fusion

    class Assembler:
        def assemble(self, **_kwargs: object) -> object:
            events.append("assembly")
            return result

    class Writer:
        def write(self, root: Path, _result: object, **_kwargs: object) -> Path:
            events.append("artifact")
            root.mkdir(parents=True)
            return root

    def reference(_root: Path, _source: str, _reference_root: Path | None) -> str:
        events.append("reference")
        return "| A | B |"

    monkeypatch.setattr(runner, "OcrEvidenceFusion", Fusion)
    monkeypatch.setattr(runner, "DocumentAssembler", Assembler)
    monkeypatch.setattr(runner, "DocumentArtifactWriter", Writer)
    monkeypatch.setattr(runner, "_reference", reference)
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    item = runner._finish_item(
        _prepared(runner),
        input_root=tmp_path,
        reference_root=None,
        corpus_dir=corpus_dir,
        sessions=Sessions(),
        require_reference=True,
    )
    assert events == ["queue", "fusion", "assembly", "artifact", "reference"]
    assert item.status == "complete"
    assert item.completed_stages == runner.PIPELINE_ORDER
    assert item.lost_characters == 0
    assert item.legacy_line_recall_percent == 100.0
    assert item.artifact == "items/page"


def test_all_ocr_jobs_failed_is_red_but_stage7_artifact_remains_auditable(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = SimpleNamespace(jobs=(object(),), complete=0, failed=1)
    fusion = SimpleNamespace(status=OcrFusionStatus.UNRESOLVED)
    result = SimpleNamespace(
        status=AssemblyStatus.UNRESOLVED,
        text=None,
        candidate_text="",
        markdown=None,
        candidate_markdown="",
        evidence_slices=(),
        structural_units=(),
    )

    class Sessions:
        def run(self, **_kwargs: object) -> object:
            return queue

    class Fusion:
        def __init__(self, _config: object) -> None:
            pass

        def fuse(self, **_kwargs: object) -> object:
            return fusion

    class Assembler:
        def assemble(self, **_kwargs: object) -> object:
            return result

    class Writer:
        def write(self, root: Path, _result: object, **_kwargs: object) -> Path:
            root.mkdir(parents=True)
            (root / "evidence-was-published.txt").write_text("yes", encoding="utf-8")
            return root

    monkeypatch.setattr(runner, "OcrEvidenceFusion", Fusion)
    monkeypatch.setattr(runner, "DocumentAssembler", Assembler)
    monkeypatch.setattr(runner, "DocumentArtifactWriter", Writer)
    monkeypatch.setattr(runner, "_reference", lambda *_args: "known")
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    item = runner._finish_item(
        _prepared(runner, blocks=(object(),)),
        input_root=tmp_path,
        reference_root=None,
        corpus_dir=corpus_dir,
        sessions=Sessions(),
        require_reference=True,
    )
    assert item.status == "failed"
    assert item.error == "all OCR jobs failed"
    assert item.artifact == "items/page"
    assert (corpus_dir / item.artifact / "evidence-was-published.txt").is_file()


def test_summary_uses_micro_loss_and_rejects_accuracy_below_default_gate(
    runner: ModuleType, tmp_path: Path
) -> None:
    items = (
        runner.CorpusItem(
            source="large.png",
            status="complete",
            geometry_status="complete",
            assembly_status="complete",
            elapsed_seconds=1.0,
            completed_stages=runner.PIPELINE_ORDER,
            stage_seconds=(),
            lost_characters=10,
            reference_characters=100,
            recognized_characters=90,
            accuracy_percent=90.0,
            artifact="items/large",
        ),
        runner.CorpusItem(
            source="small.png",
            status="complete",
            geometry_status="complete",
            assembly_status="complete",
            elapsed_seconds=2.0,
            completed_stages=runner.PIPELINE_ORDER,
            stage_seconds=(),
            lost_characters=1,
            reference_characters=1,
            recognized_characters=0,
            accuracy_percent=0.0,
            artifact="items/small",
        ),
    )
    summary = runner._write_summary(
        tmp_path,
        items=items,
        engines=("tesseract",),
        prepare_workers=4,
        ocr_page_workers=2,
        elapsed_seconds=3.0,
    )
    assert summary["gate_status"] == "RED"
    assert summary["gate_mode"] == "strict"
    assert summary["minimum_accuracy_percent"] == 91.0
    assert summary["scored_images"] == summary["images"] == 2
    assert summary["gate_reasons"] == ["micro-accuracy-below-threshold"]
    assert summary["accuracy_percent_micro"] == pytest.approx(
        100.0 * (1.0 - 11 / 101)
    )
    assert summary["accuracy_percent_mean"] == 45.0
    assert summary["totals"]["lost_characters"] == 11
    assert summary["totals"]["reference_characters"] == 101
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))[
        "accuracy_percent_micro"
    ] == pytest.approx(100.0 * (1.0 - 11 / 101))
    assert (tmp_path / "summary.tsv").is_file()
    assert (tmp_path / "summary.md").is_file()
    assert not tuple(tmp_path.glob(".*.partial"))


def test_summary_green_requires_full_scoring_zero_unresolved_and_threshold(
    runner: ModuleType, tmp_path: Path
) -> None:
    item = runner.CorpusItem(
        source="page.png",
        status="complete",
        geometry_status="complete",
        assembly_status="complete",
        elapsed_seconds=1.0,
        completed_stages=runner.PIPELINE_ORDER,
        stage_seconds=(),
        lost_characters=9,
        reference_characters=100,
        recognized_characters=91,
        accuracy_percent=91.0,
        artifact="items/page",
    )
    summary = runner._write_summary(
        tmp_path,
        items=(item,),
        engines=("tesseract",),
        prepare_workers=1,
        ocr_page_workers=1,
        elapsed_seconds=1.0,
    )
    assert summary["gate_status"] == "GREEN"
    assert summary["gate_reasons"] == []
    assert summary["invariants"]["full_reference_scoring"] is True
    assert summary["invariants"]["zero_execution_failures"] is True
    assert summary["invariants"]["zero_unresolved_items"] is True
    assert summary["invariants"]["micro_accuracy_threshold_met"] is True


def test_summary_never_calls_relaxed_missing_or_unresolved_evidence_green(
    runner: ModuleType, tmp_path: Path
) -> None:
    unscored = runner.CorpusItem(
        source="unscored.png",
        status="complete",
        geometry_status="complete",
        assembly_status="complete",
        elapsed_seconds=1.0,
        completed_stages=runner.PIPELINE_ORDER,
        stage_seconds=(),
        artifact="items/unscored",
    )
    unresolved = runner.CorpusItem(
        source="unresolved.png",
        status="unresolved",
        geometry_status="complete",
        assembly_status="unresolved",
        elapsed_seconds=1.0,
        completed_stages=runner.PIPELINE_ORDER,
        stage_seconds=(),
        lost_characters=0,
        reference_characters=10,
        recognized_characters=10,
        accuracy_percent=100.0,
        artifact="items/unresolved",
    )

    for name in ("strict-missing", "strict-unresolved", "exploratory"):
        (tmp_path / name).mkdir()

    strict_missing = runner._write_summary(
        tmp_path / "strict-missing",
        items=(unscored,),
        engines=("tesseract",),
        prepare_workers=1,
        ocr_page_workers=1,
        elapsed_seconds=1.0,
    )
    strict_unresolved = runner._write_summary(
        tmp_path / "strict-unresolved",
        items=(unresolved,),
        engines=("tesseract",),
        prepare_workers=1,
        ocr_page_workers=1,
        elapsed_seconds=1.0,
    )
    exploratory = runner._write_summary(
        tmp_path / "exploratory",
        items=(unresolved,),
        engines=("tesseract",),
        prepare_workers=1,
        ocr_page_workers=1,
        elapsed_seconds=1.0,
        gate_policy=runner.QualityGatePolicy(require_resolved=False),
    )

    assert strict_missing["gate_status"] == "RED"
    assert "reference-scoring-incomplete" in strict_missing["gate_reasons"]
    assert strict_unresolved["gate_status"] == "RED"
    assert "unresolved-items" in strict_unresolved["gate_reasons"]
    assert exploratory["gate_status"] == "EXPLORATORY"
    assert exploratory["gate_mode"] == "exploratory"


def test_evidence_only_item_must_reach_stage7_but_is_not_in_quality_denominator(
    runner: ModuleType, tmp_path: Path
) -> None:
    printed = runner.CorpusItem(
        source="printed.png",
        status="complete",
        geometry_status="complete",
        assembly_status="complete",
        elapsed_seconds=1.0,
        completed_stages=runner.PIPELINE_ORDER,
        stage_seconds=(),
        quality_required=True,
        lost_characters=5,
        reference_characters=100,
        recognized_characters=95,
        accuracy_percent=95.0,
        artifact="items/printed",
    )
    handwriting = runner.CorpusItem(
        source="handwriting.png",
        status="unresolved",
        geometry_status="complete",
        assembly_status="unresolved",
        elapsed_seconds=1.0,
        completed_stages=runner.PIPELINE_ORDER,
        stage_seconds=(),
        quality_required=False,
        artifact="items/handwriting",
    )

    summary = runner._write_summary(
        tmp_path,
        items=(printed, handwriting),
        engines=("tesseract",),
        prepare_workers=1,
        ocr_page_workers=1,
        elapsed_seconds=2.0,
    )

    assert summary["gate_status"] == "GREEN"
    assert summary["images"] == 2
    assert summary["quality_images"] == summary["scored_images"] == 1
    assert summary["evidence_only_images"] == 1
    assert summary["unresolved"] == 1
    assert summary["quality_unresolved"] == 0
    assert summary["invariants"]["all_items_reached_stage7"] is True
    assert summary["invariants"]["zero_quality_unresolved_items"] is True
    assert summary["invariants"]["zero_unresolved_items"] is False


def test_reference_lookup_rejects_disagreeing_ground_truth_files(
    runner: ModuleType, tmp_path: Path
) -> None:
    inputs = tmp_path / "input"
    references = tmp_path / "reference"
    inputs.mkdir()
    references.mkdir()
    (inputs / "page.png").write_bytes(b"not decoded")
    (inputs / "page.txt").write_text("first", encoding="utf-8")
    (references / "page.png.md").write_text("second", encoding="utf-8")
    with pytest.raises(ValueError, match="ambiguous reference files disagree"):
        runner._reference(inputs, "page.png", references)


def test_v20_wrapper_is_syntax_valid_fail_closed_and_bounded() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    wrapper = repository_root / "scripts" / "debug" / "run-sparse-v20.sh"
    completed = subprocess.run(
        ("bash", "-n", str(wrapper)),
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    source = wrapper.read_text(encoding="utf-8")
    assert "set -Eeuo pipefail" in source
    assert 'pipe_status=("${PIPESTATUS[@]}")' in source
    assert "tee_rc=${pipe_status[1]}" in source
    assert 'v20_limit="${V20_LIMIT:-24}"' in source
    assert 'if ! mkdir "$gate_dir"' in source
    assert 'mkdir -p "$gate_dir/logs"' not in source
    assert "--limit \"$v20_limit\"" in source
    assert "--fail-on-unresolved" in source
    assert '--minimum-accuracy-percent "$v20_minimum_accuracy"' in source
    assert "V20_FAIL_ON_UNRESOLVED" not in source
    assert "stage7-summary-exists" in source
    assert "scripts/debug/validate_v20_summary.py" in source
    assert 'summary_validator_args+=(--required-source "$required_source")' in source
    assert (
        'summary_validator_args+=(--evidence-only-source "$evidence_source")'
        in source
    )
    assert 'evidence_only_source = "Adobe Scan' not in source
    assert 'tutorial_set="${V20_TUTORIAL_SET:-problem}"' in source
    assert "000041301_UchebPlan_sign000029629.pdf.raster.png" in source
    assert "09.03.03_05(ИУ1).pdf.raster.png" in source
    assert "Adobe Scan Jun 20, 2026.pdf.raster.png" in source
    assert "--evidence-only-source" in source
    assert 'tutorial_inputs=("debug/fixtures/SAMPLE_4k.png")' in source
    assert "stage7-object-artifacts" in source
    assert "stage7-artifact-layout" in source
    assert "Stage 7 artifacts were preserved for diagnosis" in source
    assert '"objects/manifest.json"' in source
    assert '"sparse-matrix.tsv"' in source
    assert '"provenance.json"' in source
    assert "notify-send" in source
    assert source.index("stage3-control") < source.index("stage1-geometry")
    assert source.index("stage1-geometry") < source.index("stage6-objects")
    assert source.index("stage6-objects") < source.index("stage4-enhancement")
    assert source.index("stage4-enhancement") < source.index("stage5-blocks")
    assert source.index("stage5-blocks") < source.index("stage2-ocr")
    assert source.index("stage2-ocr") < source.index("stage7-document")
