from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_validator() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "debug"
        / "validate_v20_summary.py"
    )
    name = "_validate_v20_summary_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _summary(*, adobe_quality: bool = True) -> dict[str, object]:
    items = [
        {
            "source": "Adobe Scan Jun 20, 2026.pdf.raster.png",
            "quality_required": adobe_quality,
        },
        {
            "source": "nested/custom-unsupported.png",
            "quality_required": False,
        },
        {"source": "printed.png", "quality_required": True},
    ]
    quality_images = sum(bool(item["quality_required"]) for item in items)
    return {
        "gate_status": "GREEN",
        "gate_mode": "strict",
        "execution_order": [3, 1, 6, 4, 5, 2, 7],
        "failures": 0,
        "quality_unresolved": 0,
        "images": len(items),
        "quality_images": quality_images,
        "evidence_only_images": len(items) - quality_images,
        "scored_images": quality_images,
        "minimum_accuracy_percent": 91.0,
        "accuracy_percent_micro": 95.0,
        "invariants": {
            "full_quality_reference_scoring": True,
            "zero_execution_failures": True,
            "zero_quality_unresolved_items": True,
            "micro_accuracy_threshold_met": True,
            "all_items_reached_stage7": True,
        },
        "items": items,
    }


def test_validator_uses_the_configured_evidence_scope_not_adobe_literal() -> None:
    validator = _load_validator()

    validator.validate_summary(
        _summary(),
        required_sources=(
            "Adobe Scan Jun 20, 2026.pdf.raster.png",
            "printed.png",
        ),
        evidence_only_sources=("custom-unsupported.png",),
    )

    with pytest.raises(
        validator.V20SummaryError,
        match="wrong quality scope: Adobe Scan",
    ):
        validator.validate_summary(
            _summary(),
            evidence_only_sources=(
                "Adobe Scan Jun 20, 2026.pdf.raster.png",
            ),
        )


def test_validator_matches_nested_source_by_exact_label_or_basename() -> None:
    validator = _load_validator()

    validator.validate_summary(
        _summary(),
        evidence_only_sources=("nested/custom-unsupported.png",),
    )
    validator.validate_summary(
        _summary(),
        evidence_only_sources=("custom-unsupported.png",),
    )


def test_validator_rejects_duplicate_or_miscounted_scope() -> None:
    validator = _load_validator()

    with pytest.raises(validator.V20SummaryError, match="must be unique"):
        validator.validate_summary(
            _summary(),
            evidence_only_sources=("custom-unsupported.png",) * 2,
        )

    broken = _summary()
    broken["evidence_only_images"] = 0
    with pytest.raises(
        validator.V20SummaryError,
        match="invalid evidence-only cohort",
    ):
        validator.validate_summary(
            broken,
            evidence_only_sources=("custom-unsupported.png",),
        )
