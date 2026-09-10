from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

from app.sparse_pipeline.block_planning import BlockPlan
from app.sparse_pipeline.contracts import GeometryStatus
from app.sparse_pipeline.ocr_fusion import OcrEvidenceFusion
from app.sparse_pipeline.ocr_queue import (
    OcrQueueResult,
    OcrQueueStatus,
)


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts" / "debug" / "debug_ocr_blocks.py"
    name = "_stage2_debug_ocr_blocks_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _empty_prepared(runner: ModuleType, source: str = "page.png") -> object:
    return runner.PreparedItem(
        source=source,
        run_id=source.replace(".", "-"),
        geometry_status=GeometryStatus.COMPLETE.value,
        preparation_seconds=0.01,
        segments=(),
        plan=BlockPlan((1, 1), (), (), ()),
        crops=(),
    )


def _complete_item(runner: ModuleType, source: str) -> object:
    return runner.CorpusItem(
        source=source,
        status="complete",
        geometry_status="complete",
        preparation_seconds=0.01,
        ocr_seconds=0.02,
        total_seconds=0.03,
        lost_characters=0,
        reference_characters=4,
        recognized_characters=4,
        accuracy_percent=100.0,
    )


def test_preparation_window_is_bounded_and_uses_threads_concurrently(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = tuple(tmp_path / f"page-{index}.png" for index in range(9))
    lock = threading.Lock()
    active = 0
    peak = 0
    thread_ids: set[int] = set()

    def prepare(source: str, _input_root: str) -> object:
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
            geometry_status="complete",
            preparation_seconds=0.025,
            error="fixture-only",
        )

    monkeypatch.setattr(runner, "_prepare_item", prepare)
    prepared = tuple(
        runner._prepared_stream(
            sources,
            input_root=tmp_path,
            workers=4,
            window=3,
            executor_kind="thread",
        )
    )
    assert len(prepared) == len(sources)
    assert peak == 3
    assert len(thread_ids) >= 2
    assert active == 0


@pytest.mark.parametrize(
    ("reference", "recognized", "expected"),
    (
        (" A\tB\n中\u00a0文 ", "AB中X", (1, 4, 4, 75.0)),
        ("Latin A", "Latin А", (1, 6, 6, 100.0 * 5 / 6)),
        ("\n\t\u2003", " ", (0, 0, 0, 100.0)),
        ("abc", "a b c d", (1, 3, 4, 100.0 * 2 / 3)),
    ),
)
def test_metric_is_exact_levenshtein_after_all_unicode_whitespace_is_removed(
    runner: ModuleType,
    reference: str,
    recognized: str,
    expected: tuple[int, int, int, float],
) -> None:
    actual = runner._metric(reference, recognized)
    assert actual[:3] == expected[:3]
    assert actual[3] == pytest.approx(expected[3])


def test_reference_is_loaded_only_after_queue_fusion_and_artifact_publication(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    prepared = _empty_prepared(runner)
    queue = OcrQueueResult((), OcrQueueStatus.COMPLETE, 0, 0)
    fusion = OcrEvidenceFusion().fuse(
        plan=prepared.plan,
        segments=(),
        crops=(),
        queue=queue,
    )
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()

    class Session:
        def run(self, **_kwargs: object) -> OcrQueueResult:
            events.append("queue")
            return queue

    class Fusion:
        def fuse(self, **_kwargs: object) -> object:
            events.append("fusion")
            return fusion

    class Writer:
        def write(self, root: Path, *, run_id: str, **_kwargs: object) -> Path:
            events.append("artifact")
            destination = root / run_id
            destination.mkdir(parents=True)
            return destination

    def reference(_root: Path, _source: str) -> str:
        events.append("reference")
        return "secret reference"

    monkeypatch.setattr(runner, "OcrEvidenceFusion", Fusion)
    monkeypatch.setattr(runner, "OcrArtifactWriter", Writer)
    monkeypatch.setattr(runner, "_reference", reference)
    result = runner._run_ocr(
        prepared,
        input_root=tmp_path,
        corpus_dir=corpus_dir,
        session=Session(),
    )
    assert events == ["queue", "fusion", "artifact", "reference"]
    assert result.lost_characters == len("secretreference")
    assert result.artifact == "items/page-png"


def test_missing_reference_is_reported_as_unscored_not_as_zero_accuracy(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "nested" / "page.png"
    image.parent.mkdir()
    image.write_bytes(b"not-decoded-by-this-test")
    assert runner._reference(tmp_path, "nested/page.png") is None

    prepared = _empty_prepared(runner, "nested/page.png")
    queue = OcrQueueResult((), OcrQueueStatus.COMPLETE, 0, 0)
    fusion = OcrEvidenceFusion().fuse(
        plan=prepared.plan,
        segments=(),
        crops=(),
        queue=queue,
    )
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()

    class Session:
        def run(self, **_kwargs: object) -> OcrQueueResult:
            return queue

    class Fusion:
        def fuse(self, **_kwargs: object) -> object:
            return fusion

    class Writer:
        def write(self, root: Path, *, run_id: str, **_kwargs: object) -> Path:
            destination = root / run_id
            destination.mkdir(parents=True)
            return destination

    monkeypatch.setattr(runner, "OcrEvidenceFusion", Fusion)
    monkeypatch.setattr(runner, "OcrArtifactWriter", Writer)
    result = runner._run_ocr(
        prepared,
        input_root=tmp_path,
        corpus_dir=corpus_dir,
        session=Session(),
    )
    assert result.status == "complete"
    assert result.lost_characters is None
    assert result.reference_characters is None
    assert result.recognized_characters is None
    assert result.accuracy_percent is None


def test_run_ocr_converts_worker_failure_to_a_complete_failure_record(
    runner: ModuleType,
    tmp_path: Path,
) -> None:
    prepared = _empty_prepared(runner)

    class FailedSession:
        def run(self, **_kwargs: object) -> object:
            raise RuntimeError("hermetic worker failure")

    result = runner._run_ocr(
        prepared,
        input_root=tmp_path,
        corpus_dir=tmp_path,
        session=FailedSession(),
    )
    assert result.status == "failed"
    assert result.geometry_status == "complete"
    assert result.ocr_seconds >= 0.0
    assert result.total_seconds >= result.preparation_seconds
    assert result.error == "RuntimeError: hermetic worker failure"
    assert result.artifact == ""


def test_summary_json_tsv_and_markdown_are_complete_and_consistent(
    runner: ModuleType,
    tmp_path: Path,
) -> None:
    items = (
        _complete_item(runner, "a|page.png"),
        runner.CorpusItem(
            source="b-page.png",
            status="unresolved",
            geometry_status="complete",
            preparation_seconds=1.0,
            ocr_seconds=2.0,
            total_seconds=3.0,
            segments=3,
            blocks=2,
            jobs=8,
            complete_jobs=7,
            failed_jobs=1,
            unresolved_segments=1,
            unassigned_words=2,
            replica_conflicts=1,
            lost_characters=2,
            reference_characters=10,
            recognized_characters=9,
            accuracy_percent=80.0,
            error="line one\nline two",
        ),
    )
    summary = runner._write_summary(
        tmp_path,
        items=items,
        engines=("tesseract", "easy-ru"),
        elapsed_seconds=4.5,
    )
    on_disk = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert on_disk == summary
    assert summary["status"] == "unresolved"
    assert summary["images"] == 2
    assert summary["accuracy_percent_mean"] == 90.0
    assert summary["accuracy_percent_micro"] == pytest.approx(100.0 * (1.0 - 2 / 14))
    assert summary["totals"]["lost_characters"] == 2
    assert summary["totals"]["reference_characters"] == 14
    with (tmp_path / "summary.tsv").open(encoding="utf-8", newline="") as stream:
        rows = tuple(csv.reader(stream, delimiter="\t"))
    assert len(rows) == 3
    assert rows[0] == list(runner.CorpusItem.__dataclass_fields__)
    markdown = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Status: **unresolved**" in markdown
    assert "a\\|page.png" in markdown
    assert "line one<br>line two" in markdown


def test_main_reuses_one_persistent_session_and_sorts_report_rows(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_root = tmp_path / "inputs"
    output_root = tmp_path / "output"
    input_root.mkdir()
    sources = (input_root / "b.png", input_root / "a.png")
    args = argparse.Namespace(
        input=input_root,
        output=output_root,
        run_id="persistent-run",
        engines=("tesseract",),
        limit=None,
        prepare_workers=2,
        prepare_window=2,
        prepare_executor="thread",
        tesseract_workers=2,
        glm_workers=1,
        fail_on_unresolved=False,
    )
    sessions: list[object] = []
    seen_session_ids: list[int] = []

    class Session:
        def __init__(self, lanes: object) -> None:
            self.lanes = lanes
            sessions.append(self)

        def __enter__(self) -> object:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def prepared_stream(*_args: object, **_kwargs: object) -> object:
        yield _empty_prepared(runner, "b.png")
        yield _empty_prepared(runner, "a.png")

    def run_ocr(prepared: object, *, session: object, **_kwargs: object) -> object:
        seen_session_ids.append(id(session))
        return _complete_item(runner, prepared.source)

    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner, "_discover", lambda _root: sources)
    monkeypatch.setattr(runner, "_lanes", lambda _args: ("lane",))
    monkeypatch.setattr(runner, "_prepared_stream", prepared_stream)
    monkeypatch.setattr(runner, "_run_ocr", run_ocr)
    monkeypatch.setattr(runner, "PersistentOcrSession", Session)
    assert runner.main() == 0
    assert len(sessions) == 1
    assert seen_session_ids == [id(sessions[0]), id(sessions[0])]
    summary = json.loads((output_root / "persistent-run" / "summary.json").read_text(encoding="utf-8"))
    assert [item["source"] for item in summary["items"]] == ["a.png", "b.png"]


def test_main_returns_failure_and_writes_the_failed_item(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_root = tmp_path / "inputs"
    output_root = tmp_path / "output"
    input_root.mkdir()
    source = input_root / "failed.png"
    args = argparse.Namespace(
        input=input_root,
        output=output_root,
        run_id="failed-run",
        engines=("tesseract",),
        limit=None,
        prepare_workers=1,
        prepare_window=1,
        prepare_executor="thread",
        tesseract_workers=1,
        glm_workers=1,
        fail_on_unresolved=False,
    )

    class Session:
        def __init__(self, _lanes: object) -> None:
            pass

        def __enter__(self) -> object:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def prepared_stream(*_args: object, **_kwargs: object) -> object:
        yield _empty_prepared(runner, "failed.png")

    failed = runner.CorpusItem(
        source="failed.png",
        status="failed",
        geometry_status="error",
        preparation_seconds=0.1,
        ocr_seconds=0.0,
        total_seconds=0.1,
        error="FixtureError: reported",
    )
    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner, "_discover", lambda _root: (source,))
    monkeypatch.setattr(runner, "_lanes", lambda _args: ())
    monkeypatch.setattr(runner, "_prepared_stream", prepared_stream)
    monkeypatch.setattr(runner, "_run_ocr", lambda *_args, **_kwargs: failed)
    monkeypatch.setattr(runner, "PersistentOcrSession", Session)
    assert runner.main() == 1
    summary = json.loads((output_root / "failed-run" / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert summary["failures"] == 1
    assert summary["items"][0]["error"] == "FixtureError: reported"
