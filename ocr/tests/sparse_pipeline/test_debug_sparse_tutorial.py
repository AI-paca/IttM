from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_runner() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts" / "debug" / "debug_sparse_tutorial.py"
    name = "_debug_sparse_tutorial_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_report_separates_quality_from_evidence_certification() -> None:
    runner = _load_runner()
    item = runner.TutorialItem(
        source="sample.png",
        page=1,
        item_id="sample-page-001",
        status="unresolved",
        artifact="",
        elapsed_seconds=1.0,
        geometry_status="complete",
        fusion_status="unresolved",
        assembly_status="unresolved",
        segments=10,
        objects=2,
        unresolved_segments=1,
        overlap_conflicts=1,
        accuracy_percent=95.0,
    )

    report = runner._report_markdown(
        run_id="quality-green-evidence-red",
        run_dir=runner.REPOSITORY_ROOT / "debug" / "tutorial" / "report-test",
        items=(item,),
        threshold=91.0,
    )

    assert "Gate: **RED**" in report
    assert "OCR quality: **GREEN**" in report
    assert "evidence certification: **RED**" in report
    assert "| 10 | 2 | 1 | 0 | 1 |" in report


def test_report_accepts_unresolved_evidence_only_after_full_stage_publication() -> None:
    runner = _load_runner()
    quality = runner.TutorialItem(
        source="printed.png",
        page=1,
        item_id="printed-page-001",
        status="complete",
        artifact="debug/items/printed",
        elapsed_seconds=1.0,
        quality_required=True,
        accuracy_percent=95.0,
    )
    unsupported = runner.TutorialItem(
        source="handwriting.png",
        page=1,
        item_id="handwriting-page-001",
        status="unresolved",
        artifact="debug/items/handwriting",
        elapsed_seconds=1.0,
        quality_required=False,
    )

    report = runner._report_markdown(
        run_id="scoped-quality",
        run_dir=runner.REPOSITORY_ROOT / "debug" / "tutorial" / "scoped",
        items=(quality, unsupported),
        threshold=91.0,
    )

    assert "Gate: **GREEN**" in report
    assert "OCR quality: **GREEN**" in report
    assert "evidence certification: **GREEN**" in report
    assert "evidence-only: **1**" in report
    assert "| handwriting.png | 1 | evidence-only | unresolved |" in report


def test_stage_links_keep_manifests_but_do_not_embed_overlay_or_one_crop(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    artifact = tmp_path / "item"
    stage = artifact / "05-blocks"
    for relative in (
        "manifest.json",
        "adjacent-pairs/pair-000000/manifest.json",
        "page-block-overlay.png",
        "raw/block-000000.png",
        "gamma/block-000000.png",
    ):
        path = stage / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"artifact")

    links = runner._stage_links(artifact, "05-blocks")

    assert links.index(stage / "manifest.json") < links.index(stage / "adjacent-pairs/pair-000000/manifest.json")
    assert stage / "page-block-overlay.png" not in links
    assert stage / "raw/block-000000.png" not in links
    assert stage / "gamma/block-000000.png" not in links


def test_actual_galleries_embed_first_eight_individual_crops(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    runner.REPOSITORY_ROOT = tmp_path
    artifact = tmp_path / "item"
    segment_root = artifact / "01-geometry/segment-crops"
    block_root = artifact / "05-blocks"
    segment_items = []
    block_items = []
    for index in range(9):
        segment_id = f"segment-{index:06d}"
        block_id = f"block-{index:06d}"
        segment_items.append(
            {
                "segment_id": segment_id,
                "bbox": [index, 1, index + 2, 4],
                "width": 2,
                "height": 3,
                "ink_pixels": 2,
                "ownership_pixels": 2,
                "sparse_cells": [[index, 0]],
                "sparse_span": {
                    "row_start": index,
                    "row_stop": index + 1,
                    "column_start": 0,
                    "column_stop": 1,
                },
                "raw": f"raw/{segment_id}.png",
                "isolated": f"isolated/{segment_id}.png",
            }
        )
        block_items.append(
            {
                "block_id": block_id,
                "bbox": [index, 1, index + 2, 4],
                "width": 2,
                "height": 3,
                "core_segment_ids": [segment_id],
                "context_segment_ids": [],
                "segment_ids": [segment_id],
                "raw": f"raw/{block_id}.png",
                "gamma": f"gamma/{block_id}.png",
            }
        )
    segment_root.mkdir(parents=True)
    block_root.mkdir(parents=True, exist_ok=True)
    (segment_root / "manifest.json").write_text(json.dumps({"items": segment_items}), encoding="utf-8")
    (block_root / "crop-gallery.json").write_text(json.dumps({"items": block_items}), encoding="utf-8")

    segments = "\n".join(runner._actual_segment_gallery(artifact))
    blocks = "\n".join(runner._actual_block_gallery(artifact))

    assert segments.count("##### `segment-") == 8
    assert blocks.count("##### `block-") == 8
    assert "segment-000007" in segments
    assert "segment-000008" not in segments
    assert "block-000007" in blocks
    assert "block-000008" not in blocks
    assert "page-block-overlay" not in blocks


def test_report_links_resolve_from_root_and_run_directory(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    runner.REPOSITORY_ROOT = tmp_path
    run_dir = tmp_path / "debug" / "tutorial" / "link-test"
    artifact = run_dir / "items" / "item-page-001"

    files = (
        artifact / "tutorial.md",
        artifact / "sparse-matrix.txt",
        artifact / "sparse-matrix-ownership.png",
        artifact / "objects/index.md",
        artifact / "logs/stages.tsv",
        artifact / "provenance.json",
        artifact / "document.md",
        artifact / "01-geometry/manifest.json",
        artifact / "01-geometry/segment-crops/gallery.md",
        artifact / "01-geometry/segment-crops/raw/segment-000000.png",
        artifact / "01-geometry/segment-crops/isolated/segment-000000.png",
        artifact / "03-control/manifest.json",
        artifact / "04-enhancement/manifest.json",
        artifact / "04-enhancement/production-block-gallery.md",
        artifact / "04-enhancement/raw/block-000000.png",
        artifact / "04-enhancement/gamma/block-000000.png",
        artifact / "05-blocks/manifest.json",
        artifact / "05-blocks/membership-gallery.md",
        artifact / "05-blocks/raw/block-000000.png",
        artifact / "06-objects/manifest.json",
        artifact / "02-ocr/manifest.json",
        artifact / "07-document/document.md",
        run_dir / "summary.json",
    )
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}" if path.suffix == ".json" else b"artifact")

    segment_manifest = artifact / "01-geometry/segment-crops/manifest.json"
    segment_manifest.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "segment_id": "segment-000000",
                        "bbox": [0, 0, 10, 10],
                        "width": 10,
                        "height": 10,
                        "ink_pixels": 20,
                        "ownership_pixels": 20,
                        "sparse_cells": [[0, 0]],
                        "sparse_span": {
                            "row_start": 0,
                            "row_stop": 1,
                            "column_start": 0,
                            "column_stop": 1,
                        },
                        "raw": "raw/segment-000000.png",
                        "isolated": "isolated/segment-000000.png",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (artifact / "04-enhancement/production-block-manifest.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "block_id": "block-000000",
                        "bbox": [0, 0, 10, 10],
                        "width": 10,
                        "height": 10,
                        "recipe": "test",
                        "backend": "numpy",
                        "same_geometry": True,
                        "source": "raw/block-000000.png",
                        "output": "gamma/block-000000.png",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (artifact / "05-blocks/crop-gallery.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "block_id": "block-000000",
                        "bbox": [0, 0, 10, 10],
                        "width": 10,
                        "height": 10,
                        "core_segment_ids": ["segment-000000"],
                        "context_segment_ids": [],
                        "segment_ids": ["segment-000000"],
                        "raw": "raw/block-000000.png",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    item = runner.TutorialItem(
        source="printed.png",
        page=1,
        item_id="item-page-001",
        status="complete",
        artifact=artifact.relative_to(tmp_path).as_posix(),
        elapsed_seconds=1.0,
        geometry_status="complete",
        fusion_status="complete",
        assembly_status="complete",
        accuracy_percent=100.0,
    )
    root_report_path = tmp_path / "debag-report.md"
    run_report_path = run_dir / "debag-report.md"
    reports = (
        (
            root_report_path,
            runner._report_markdown(
                run_id="link-test",
                run_dir=run_dir,
                items=(item,),
                threshold=91.0,
                report_path=root_report_path,
            ),
        ),
        (
            run_report_path,
            runner._report_markdown(
                run_id="link-test",
                run_dir=run_dir,
                items=(item,),
                threshold=91.0,
                report_path=run_report_path,
            ),
        ),
    )

    for report_path, report in reports:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
        targets = re.findall(r"!?\[[^]]*\]\(([^)]+)\)", report)
        assert targets
        for target in targets:
            assert not Path(target).is_absolute()
            assert (report_path.parent / target).resolve(strict=True).is_file()

    assert "debug/tutorial/link-test/items/" in reports[0][1]
    assert "items/item-page-001/" in reports[1][1]
    assert "debug/tutorial/link-test/items/" not in reports[1][1]


def test_engine_parser_accepts_a_unique_comma_separated_lane_set() -> None:
    runner = _load_runner()

    args = runner._parser().parse_args(
        [
            "--run-id",
            "multi-engine",
            "--engines",
            "tesseract,easy-ru",
            "--easy-python",
            "/opt/easy/bin/python",
            "--easy-models",
            "/models/easyocr",
            "--easy-device",
            "cpu",
        ]
    )

    assert args.engines == ("tesseract", "easy-ru")
    assert args.easy_python == Path("/opt/easy/bin/python")
    assert args.easy_models == Path("/models/easyocr")
    assert args.easy_device == "cpu"
    with pytest.raises(argparse.ArgumentTypeError, match="unique"):
        runner._engines("tesseract,tesseract")
    with pytest.raises(argparse.ArgumentTypeError, match="unknown engines"):
        runner._engines("tesseract,unknown")


def test_item_ids_hash_the_normalized_path_not_only_the_basename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    runner.REPOSITORY_ROOT = tmp_path
    monkeypatch.chdir(tmp_path)
    first = Path("first/repeated.png")
    second = Path("second/repeated.png")

    first_id = runner._item_id(first, 1)

    assert first_id == runner._item_id(first.resolve(), 1)
    assert first_id != runner._item_id(second, 1)
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", first_id)
    with pytest.raises(ValueError, match="resolve to unique files"):
        runner._validate_unique_sources((first, first.resolve()))


def test_external_artifact_paths_round_trip_without_repo_relative_crash(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    repository = tmp_path / "repository"
    repository.mkdir()
    runner.REPOSITORY_ROOT = repository
    external = tmp_path / "external-output" / "run" / "items" / "item"

    stored = runner._display_path(external)

    assert Path(stored).is_absolute()
    assert runner._artifact_path(stored) == external.resolve()
    internal = repository / "debug" / "items" / "item"
    assert runner._display_path(internal) == "debug/items/item"
    assert runner._artifact_path("debug/items/item") == internal


def test_multi_engine_lanes_and_provenance_are_built_without_inference() -> None:
    runner = _load_runner()
    args = runner._parser().parse_args(
        [
            "--run-id",
            "multi-engine",
            "--engines",
            "tesseract,easy-ru",
            "--single-context-psm",
            "6",
            "--document-context-psm",
            "4",
            "--tesseract-workers",
            "3",
            "--easy-python",
            "/opt/easy/bin/python",
            "--easy-models",
            "/models/easyocr",
            "--easy-device",
            "cpu",
        ]
    )

    single = runner._ocr_lanes(args, document_context=False)
    document = runner._ocr_lanes(args, document_context=True)
    assert tuple(lane.lane_id for lane in single) == (
        "tesseract-single-context-psm6",
        "easyocr-en-ru",
    )
    assert tuple(lane.lane_id for lane in document) == (
        "tesseract-document-context-psm4",
        "easyocr-en-ru",
    )
    assert tuple(lane.max_workers for lane in single) == (3, 1)

    # Constructing and closing the persistent sessions must not initialize an
    # OCR model; workers are created lazily only when a crop is submitted.
    with (
        runner.PersistentOcrSession(single) as single_session,
        runner.PersistentOcrSession(document) as document_session,
    ):
        assert single_session.lanes == single
        assert document_session.lanes == document

    provenance = runner._ocr_provenance(
        args,
        single_lanes=single,
        document_lanes=document,
    )
    assert provenance["engines"] == ["tesseract", "easy-ru"]
    assert provenance["ocr_profile"] == {
        "single_context": [
            "tesseract-single-context-psm6",
            "easyocr-en-ru",
        ],
        "document_context": [
            "tesseract-document-context-psm4",
            "easyocr-en-ru",
        ],
    }
    assert provenance["easyocr"] == {
        "python": "/opt/easy/bin/python",
        "models": "/models/easyocr",
        "device": "cpu",
        "runtime_downloads": False,
    }

    item = runner.TutorialItem(
        source="printed.png",
        page=1,
        item_id="printed-page-001",
        status="complete",
        artifact="debug/items/printed",
        elapsed_seconds=1.0,
        ocr_profile=tuple(lane.lane_id for lane in document),
    )
    encoded = json.loads(json.dumps(runner.asdict(item)))
    assert encoded["ocr_profile"] == [
        "tesseract-document-context-psm4",
        "easyocr-en-ru",
    ]


def test_v20_wrapper_passes_the_same_engine_and_easyocr_configuration() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    source = (repository_root / "scripts" / "debug" / "run-sparse-v20.sh").read_text(encoding="utf-8")

    assert source.count('--engines "${V20_ENGINES:-tesseract}"') == 2
    assert (
        source.count(
            '--easy-python "${V20_EASY_PYTHON:-/home/alpaca/GitHub/' 'IttM-engine-original/ocr/.venv/bin/python}"'
        )
        == 2
    )
    assert source.count('--easy-models "${V20_EASY_MODELS:-/home/alpaca/.EasyOCR/model}"') == 2
    assert source.count('--easy-device "${V20_EASY_DEVICE:-cuda}"') == 2
    assert '--tesseract-psm "${V20_SINGLE_CONTEXT_PSM:-6}"' in source
    assert '--single-context-psm "${V20_SINGLE_CONTEXT_PSM:-6}"' in source
    assert '--document-context-psm "${V20_DOCUMENT_CONTEXT_PSM:-4}"' in source
