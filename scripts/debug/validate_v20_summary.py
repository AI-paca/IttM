#!/usr/bin/env python3
"""Fail-closed validation for the Stage 7 v20 corpus summary."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path, PurePath
from typing import Any, Mapping, Sequence


class V20SummaryError(ValueError):
    """The persisted Stage 7 summary does not satisfy the v20 contract."""


def _scope_matches(source: str, configured: Sequence[str]) -> bool:
    return source in configured or PurePath(source).name in configured


def validate_summary(
    summary: Mapping[str, Any],
    *,
    required_sources: Sequence[str] = (),
    evidence_only_sources: Sequence[str] = (),
) -> None:
    """Validate execution, quality, and the exact configured quality scope."""

    if len(required_sources) != len(set(required_sources)):
        raise V20SummaryError("required source values must be unique")
    if len(evidence_only_sources) != len(set(evidence_only_sources)):
        raise V20SummaryError("evidence-only source values must be unique")
    if any(not value for value in (*required_sources, *evidence_only_sources)):
        raise V20SummaryError("source selectors must not be empty")

    raw_items = summary.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise V20SummaryError("Stage 7 summary has no item evidence")
    if any(not isinstance(item, dict) for item in raw_items):
        raise V20SummaryError("Stage 7 summary contains a non-object item")
    items = tuple(raw_items)
    sources = tuple(item.get("source") for item in items)
    if any(not isinstance(source, str) or not source for source in sources):
        raise V20SummaryError("Stage 7 item has no valid source label")
    if len(sources) != len(set(sources)):
        raise V20SummaryError("Stage 7 summary contains duplicate source labels")

    observed_sources = set(sources)
    missing_sources = sorted(set(required_sources) - observed_sources)
    if missing_sources:
        raise V20SummaryError(
            "Stage 7 did not execute required failure-corpus inputs: "
            + ", ".join(missing_sources)
        )

    expected_quality_images = 0
    for source, item in zip(sources, items, strict=True):
        expected_quality = not _scope_matches(source, evidence_only_sources)
        if item.get("quality_required") is not expected_quality:
            raise V20SummaryError(
                f"Stage 7 source has wrong quality scope: {source}"
            )
        expected_quality_images += int(expected_quality)

    if summary.get("gate_status") != "GREEN":
        raise V20SummaryError("Stage 7 summary is not GREEN")
    if summary.get("gate_mode") != "strict":
        raise V20SummaryError("Stage 7 summary is not using the strict gate")
    if summary.get("execution_order") != [3, 1, 6, 4, 5, 2, 7]:
        raise V20SummaryError("Stage 7 execution order is not exact")
    if summary.get("failures") != 0:
        raise V20SummaryError("Stage 7 summary contains execution failures")
    if summary.get("quality_unresolved") != 0:
        raise V20SummaryError("Stage 7 summary contains unresolved quality items")

    images = summary.get("images")
    quality_images = summary.get("quality_images")
    evidence_only_images = summary.get("evidence_only_images")
    scored_images = summary.get("scored_images")
    if type(images) is not int or images != len(items):
        raise V20SummaryError("Stage 7 summary has an invalid image count")
    if (
        type(quality_images) is not int
        or quality_images != expected_quality_images
        or not 0 < quality_images <= images
    ):
        raise V20SummaryError("Stage 7 summary has an invalid quality cohort")
    if (
        type(evidence_only_images) is not int
        or evidence_only_images != images - quality_images
    ):
        raise V20SummaryError("Stage 7 summary has an invalid evidence-only cohort")
    if type(scored_images) is not int or scored_images != quality_images:
        raise V20SummaryError(
            "Stage 7 quality cohort does not have 100% reference coverage"
        )

    minimum = summary.get("minimum_accuracy_percent")
    accuracy = summary.get("accuracy_percent_micro")
    if (
        isinstance(minimum, bool)
        or not isinstance(minimum, (int, float))
        or not math.isfinite(minimum)
        or minimum < 91.0
    ):
        raise V20SummaryError("Stage 7 minimum accuracy is weaker than v20 policy")
    if (
        isinstance(accuracy, bool)
        or not isinstance(accuracy, (int, float))
        or not math.isfinite(accuracy)
        or accuracy < minimum
    ):
        raise V20SummaryError("Stage 7 micro accuracy is below its minimum")

    invariants = summary.get("invariants")
    if not isinstance(invariants, dict):
        raise V20SummaryError("Stage 7 summary invariants are missing")
    for name in (
        "full_quality_reference_scoring",
        "zero_execution_failures",
        "zero_quality_unresolved_items",
        "micro_accuracy_threshold_met",
        "all_items_reached_stage7",
    ):
        if invariants.get(name) is not True:
            raise V20SummaryError(f"Stage 7 invariant is not true: {name}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--required-source", action="append", default=[])
    parser.add_argument("--evidence-only-source", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    value = json.loads(args.summary.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise V20SummaryError("Stage 7 summary root must be an object")
    validate_summary(
        value,
        required_sources=tuple(args.required_source),
        evidence_only_sources=tuple(args.evidence_only_source),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
