#!/usr/bin/env python3
"""Audit saved v1..v19 OCR results against the current references.

The command is read-only with respect to saved runs: it never starts OCR and it
never uses generated/version-local references.  Published inventories and saved
execution records are validated before the current ``debug_report`` scorer is
allowed to contribute to a run-level metric.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import math
import multiprocessing
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
SCORER_PATH = ROOT / "scripts" / "debug" / "debug_report.py"
sys.path.insert(0, str(SCORER_PATH.parent))

from debug_report import expected_match, result_body  # noqa: E402


VERSIONS = tuple(range(1, 20))
SUITES = ("full-pdf-native", "full-pdf-as-png", "image-fixtures")
ENGINES = ("tesseract", "easyocr", "browser-tesseract")
SUITE_ORDER = {suite: index for index, suite in enumerate(SUITES)}
ENGINE_ORDER = {engine: index for index, engine in enumerate(ENGINES)}
COHORT_SCHEMA = "ittm-current-reference-cohort-v1"


@dataclass(frozen=True)
class RunSpec:
    version: int
    suite: str
    engine: str
    output_root: Path
    artifact_root: Path
    provenance: str
    saved_rerun_of: str = ""


@dataclass(frozen=True)
class FileAudit:
    version: str
    suite: str
    engine: str
    provenance: str
    file: str
    status: str
    reason: str
    reference_sha256: str
    actual_sha256: str
    match_percent: str
    matched_expected_lines: str
    total_expected_lines: str
    wall_seconds: str
    actual_path: str
    reference_path: str


@dataclass(frozen=True)
class RunAudit:
    version: str
    suite: str
    engine: str
    provenance: str
    saved_rerun_of: str
    status: str
    cohort_id: str
    cohort_files: int
    inventory_files: int
    scored_files: int
    missing_current_references: int
    missing_outputs: int
    failed_saved_rows: int
    score_errors: int
    extra_outputs: int
    micro_match_percent: str
    macro_match_percent: str
    matched_expected_lines: str
    total_expected_lines: str
    mean_wall_seconds: str
    artifact_result_sha256: str
    scorer_sha256: str
    reference_manifest_json: str
    issues: str
    output_root: str
    artifact_root: str


@dataclass(frozen=True)
class CohortAudit:
    cohort_id: str
    file_count: int
    files_json: str
    reference_manifest_json: str


@dataclass(frozen=True)
class AuditBundle:
    runs: tuple[RunAudit, ...]
    files: tuple[FileAudit, ...]
    cohorts: tuple[CohortAudit, ...]
    scorer_sha256: str
    reference_root: Path


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _published_engines(result_csv: Path) -> tuple[str, ...]:
    if not result_csv.is_file():
        return ()
    with result_csv.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, ())
    return tuple(
        engine
        for engine in ENGINES
        if any(column == engine or column.startswith(f"{engine} ") for column in header)
    )


def _summary_engines(summary_path: Path) -> tuple[str, ...]:
    if not summary_path.is_file():
        return ()
    with summary_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle, delimiter="\t")
        found = {row.get("engine", "") for row in rows}
    return tuple(engine for engine in ENGINES if engine in found)


def discover_run_specs(legacy_repo: Path, v18_repo: Path) -> tuple[RunSpec, ...]:
    """Discover the three historical layouts without treating tmp refs as truth."""

    specs: list[RunSpec] = []
    for version in range(1, 18):
        for suite in SUITES:
            output_root = legacy_repo / "debug" / "tmp" / f"{suite}-v{version}"
            artifact_root = legacy_repo / "debug" / "artifacts" / f"{suite}-v{version}"
            if not output_root.is_dir() and not artifact_root.is_dir():
                continue
            engines = _published_engines(artifact_root / "result.csv")
            if not engines:
                engines = _summary_engines(output_root / "summary.tsv")
            for engine in engines:
                specs.append(
                    RunSpec(
                        version=version,
                        suite=suite,
                        engine=engine,
                        output_root=output_root,
                        artifact_root=artifact_root,
                        provenance="saved_v1_v17",
                    )
                )

    for suite in SUITES:
        for engine in ENGINES:
            output_root = v18_repo / "debug" / "tmp" / f"{suite}-v18-{engine}"
            artifact_root = (
                v18_repo / "debug" / "artifacts" / f"{suite}-v18-{engine}"
            )
            if not output_root.is_dir() and not artifact_root.is_dir():
                continue
            specs.append(
                RunSpec(
                    version=18,
                    suite=suite,
                    engine=engine,
                    output_root=output_root,
                    artifact_root=artifact_root,
                    provenance="saved_v18_split_engine",
                )
            )

    for suite in SUITES:
        for engine in ENGINES:
            output_root = v18_repo / "debug" / "tmp" / f"{suite}-par-{engine}"
            artifact_root = (
                v18_repo
                / "debug"
                / "artifacts"
                / "v19"
                / engine
                / suite
            )
            if not output_root.is_dir() and not artifact_root.is_dir():
                continue
            v18_result = (
                v18_repo
                / "debug"
                / "artifacts"
                / f"{suite}-v18-{engine}"
                / "result.csv"
            )
            v19_result = artifact_root / "result.csv"
            saved_rerun_of = ""
            if v18_result.is_file() and v19_result.is_file():
                if _sha256_file(v18_result) == _sha256_file(v19_result):
                    saved_rerun_of = "v18-identical-published-csv"
            specs.append(
                RunSpec(
                    version=19,
                    suite=suite,
                    engine=engine,
                    output_root=output_root,
                    artifact_root=artifact_root,
                    provenance="saved_rerun_from_v18_offline",
                    saved_rerun_of=saved_rerun_of,
                )
            )

    return tuple(
        sorted(
            specs,
            key=lambda item: (
                item.version,
                SUITE_ORDER[item.suite],
                ENGINE_ORDER[item.engine],
            ),
        )
    )


def _read_result_inventory(path: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    issues: list[str] = []
    if not path.is_file():
        return (), ("missing_result_csv",)
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or "file" not in reader.fieldnames:
                return (), ("result_csv_missing_file_column",)
            names = [row.get("file", "").strip() for row in reader]
    except (OSError, csv.Error, UnicodeError) as exc:
        return (), (f"result_csv_read_error:{type(exc).__name__}",)

    if any(not name for name in names):
        issues.append("result_csv_empty_file")
    names = [name for name in names if name]
    unsafe = [name for name in names if Path(name).name != name]
    if unsafe:
        issues.append(f"result_csv_non_basename:{len(unsafe)}")
        names = [name for name in names if Path(name).name == name]
    duplicate_count = len(names) - len(set(names))
    if duplicate_count:
        issues.append(f"result_csv_duplicate_files:{duplicate_count}")
    return tuple(sorted(set(names))), tuple(issues)


def _read_summary(path: Path, engine: str) -> tuple[dict[str, dict[str, str]], str]:
    if not path.is_file():
        return {}, "summary_unavailable"
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            required = {"engine", "file", "http_status", "curl_exit"}
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                return {}, "summary_malformed"
            selected = [row for row in reader if row.get("engine") == engine]
    except (OSError, csv.Error, UnicodeError):
        return {}, "summary_read_error"
    if not selected:
        return {}, "summary_unavailable"
    rows: dict[str, dict[str, str]] = {}
    for row in selected:
        name = row.get("file", "")
        if not name or name in rows:
            return {}, "summary_duplicate_or_empty_file"
        rows[name] = row
    return rows, ""


def _inventory_fallback(spec: RunSpec) -> tuple[str, ...]:
    summary, _ = _read_summary(spec.output_root / "summary.tsv", spec.engine)
    if summary:
        return tuple(sorted(summary))
    engine_root = spec.output_root / spec.engine
    if not engine_root.is_dir():
        return ()
    return tuple(
        sorted(path.name.removesuffix(".md") for path in engine_root.glob("*.md"))
    )


def _read_time_rows(path: Path, engine: str) -> dict[str, float]:
    if not path.is_file():
        return {}
    column = f"{engine} seconds"
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or "file" not in reader.fieldnames:
                return {}
            if column not in reader.fieldnames:
                return {}
            values: dict[str, float] = {}
            for row in reader:
                try:
                    value = float(row.get(column, ""))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value) and value >= 0:
                    values[row.get("file", "")] = value
            return values
    except (OSError, csv.Error, UnicodeError):
        return {}


def _cohort_identity(
    reference_digests: dict[str, str],
) -> tuple[str, str, str]:
    manifest = {name: reference_digests[name] for name in sorted(reference_digests)}
    manifest_json = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    canonical = json.dumps(
        {"schema": COHORT_SCHEMA, "references": manifest},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    cohort_id = _sha256_bytes(canonical) if manifest else "n/a"
    files_json = json.dumps(tuple(manifest), ensure_ascii=False, separators=(",", ":"))
    return cohort_id, files_json, manifest_json


def _format_percent(value: float) -> str:
    return f"{value:.6f}"


def _saved_row_succeeded(row: dict[str, str] | None) -> bool:
    return bool(
        row
        and row.get("http_status") == "200"
        and row.get("curl_exit") == "0"
    )


def audit_run(
    spec: RunSpec,
    reference_root: Path,
    scorer_sha256: str,
) -> tuple[RunAudit, tuple[FileAudit, ...], CohortAudit | None]:
    issues: list[str] = []
    result_path = spec.artifact_root / "result.csv"
    inventory, inventory_issues = _read_result_inventory(result_path)
    issues.extend(inventory_issues)
    published_inventory = bool(inventory) or not inventory_issues
    if not inventory and inventory_issues:
        inventory = _inventory_fallback(spec)

    summary, summary_issue = _read_summary(
        spec.output_root / "summary.tsv", spec.engine
    )
    if summary_issue and summary_issue != "summary_unavailable":
        issues.append(summary_issue)
    time_rows = _read_time_rows(spec.artifact_root / "time.csv", spec.engine)
    engine_root = spec.output_root / spec.engine
    actual_names = (
        {path.name.removesuffix(".md") for path in engine_root.glob("*.md")}
        if engine_root.is_dir()
        else set()
    )
    extra_outputs = len(actual_names.difference(inventory))

    reference_digests: dict[str, str] = {}
    for name in inventory:
        reference_path = reference_root / f"{name}.md"
        if reference_path.is_file():
            reference_digests[name] = _sha256_file(reference_path)
    cohort_id, files_json, manifest_json = _cohort_identity(reference_digests)
    cohort = (
        CohortAudit(
            cohort_id=cohort_id,
            file_count=len(reference_digests),
            files_json=files_json,
            reference_manifest_json=manifest_json,
        )
        if reference_digests
        else None
    )

    files: list[FileAudit] = []
    missing_outputs = 0
    failed_saved_rows = 0
    score_errors = 0
    missing_references = 0
    percentages: list[float] = []
    matched_total = 0
    expected_total = 0
    cohort_times: list[float] = []
    cohort_times_complete = True
    summary_is_authoritative = bool(summary)

    for name in inventory:
        actual_path = engine_root / f"{name}.md"
        reference_path = reference_root / f"{name}.md"
        reference_digest = reference_digests.get(name, "")
        actual_digest = ""
        status = "scored"
        reason = ""
        match_percent = "n/a"
        matched_lines = ""
        total_lines = ""
        wall_value: float | None = None

        summary_row = summary.get(name) if summary_is_authoritative else None
        if summary_is_authoritative:
            if summary_row is None or not _saved_row_succeeded(summary_row):
                failed_saved_rows += 1
                status = "error"
                reason = (
                    "missing_saved_execution_row"
                    if summary_row is None
                    else "saved_execution_failed"
                )
            else:
                try:
                    wall_ms = float(summary_row.get("wall_ms", ""))
                    if math.isfinite(wall_ms) and wall_ms >= 0:
                        wall_value = wall_ms / 1000.0
                except (TypeError, ValueError):
                    pass

        if not actual_path.is_file():
            missing_outputs += 1
            status = "error"
            reason = "missing_saved_markdown"
        else:
            try:
                actual_digest = _sha256_file(actual_path)
            except OSError:
                status = "error"
                reason = "saved_markdown_read_error"

        if not reference_path.is_file():
            missing_references += 1
            if status != "error":
                status = "n/a"
                reason = "missing_current_reference"
        elif status != "error":
            try:
                match_percent, matched_lines, total_lines = expected_match(
                    result_body(actual_path), result_body(reference_path)
                )
                if match_percent == "n/a" or not total_lines:
                    raise ValueError("current reference has no scoreable lines")
                percentage = float(match_percent)
                matched = int(matched_lines)
                total = int(total_lines)
                if not math.isfinite(percentage) or total <= 0:
                    raise ValueError("invalid current scorer result")
                percentages.append(percentage)
                matched_total += matched
                expected_total += total
            except (OSError, UnicodeError, ValueError) as exc:
                score_errors += 1
                status = "error"
                reason = f"score_error:{type(exc).__name__}"
                match_percent = "n/a"
                matched_lines = ""
                total_lines = ""

        if wall_value is None:
            wall_value = time_rows.get(name)
        if reference_digest:
            if wall_value is None:
                cohort_times_complete = False
            else:
                cohort_times.append(wall_value)
        files.append(
            FileAudit(
                version=f"v{spec.version}",
                suite=spec.suite,
                engine=spec.engine,
                provenance=spec.provenance,
                file=name,
                status=status,
                reason=reason,
                reference_sha256=reference_digest,
                actual_sha256=actual_digest,
                match_percent=match_percent,
                matched_expected_lines=matched_lines,
                total_expected_lines=total_lines,
                wall_seconds=(
                    f"{wall_value:.6f}" if wall_value is not None else "n/a"
                ),
                actual_path=str(actual_path),
                reference_path=str(reference_path),
            )
        )

    integrity_error = bool(
        inventory_issues
        or (summary_issue not in {"", "summary_unavailable"})
        or missing_outputs
        or failed_saved_rows
        or score_errors
    )
    scored_files = len(percentages)
    if not inventory:
        status = "n/a"
        issues.append("empty_saved_inventory")
    elif integrity_error:
        status = "error"
    elif not reference_digests:
        status = "n/a"
        issues.append("no_current_reference_in_inventory")
    elif scored_files != len(reference_digests):
        status = "error"
        issues.append("incomplete_current_cohort")
    elif missing_references:
        status = "partial_reference"
    else:
        status = "ok"

    if summary_issue == "summary_unavailable":
        issues.append("summary_unavailable_markdown_validated_only")
    if not published_inventory:
        issues.append("inventory_fallback_is_diagnostic_only")
    if extra_outputs:
        issues.append(f"ignored_outputs_outside_inventory:{extra_outputs}")

    publish_metrics = status in {"ok", "partial_reference"}
    micro = (
        _format_percent(matched_total / expected_total * 100.0)
        if publish_metrics and expected_total
        else "n/a"
    )
    macro = (
        _format_percent(sum(percentages) / len(percentages))
        if publish_metrics and percentages
        else "n/a"
    )
    mean_wall = (
        f"{sum(cohort_times) / len(cohort_times):.6f}"
        if publish_metrics
        and cohort_times_complete
        and len(cohort_times) == len(reference_digests)
        and cohort_times
        else "n/a"
    )
    artifact_digest = _sha256_file(result_path) if result_path.is_file() else ""
    run = RunAudit(
        version=f"v{spec.version}",
        suite=spec.suite,
        engine=spec.engine,
        provenance=spec.provenance,
        saved_rerun_of=spec.saved_rerun_of,
        status=status,
        cohort_id=cohort_id,
        cohort_files=len(reference_digests),
        inventory_files=len(inventory),
        scored_files=scored_files,
        missing_current_references=missing_references,
        missing_outputs=missing_outputs,
        failed_saved_rows=failed_saved_rows,
        score_errors=score_errors,
        extra_outputs=extra_outputs,
        micro_match_percent=micro,
        macro_match_percent=macro,
        matched_expected_lines=str(matched_total) if publish_metrics else "",
        total_expected_lines=str(expected_total) if publish_metrics else "",
        mean_wall_seconds=mean_wall,
        artifact_result_sha256=artifact_digest,
        scorer_sha256=scorer_sha256,
        reference_manifest_json=manifest_json,
        issues=";".join(dict.fromkeys(issues)),
        output_root=str(spec.output_root),
        artifact_root=str(spec.artifact_root),
    )
    return run, tuple(files), cohort


def _missing_suite_run(version: int, suite: str, scorer_sha256: str) -> RunAudit:
    return RunAudit(
        version=f"v{version}",
        suite=suite,
        engine="n/a",
        provenance="not_saved",
        saved_rerun_of="",
        status="n/a",
        cohort_id="n/a",
        cohort_files=0,
        inventory_files=0,
        scored_files=0,
        missing_current_references=0,
        missing_outputs=0,
        failed_saved_rows=0,
        score_errors=0,
        extra_outputs=0,
        micro_match_percent="n/a",
        macro_match_percent="n/a",
        matched_expected_lines="",
        total_expected_lines="",
        mean_wall_seconds="n/a",
        artifact_result_sha256="",
        scorer_sha256=scorer_sha256,
        reference_manifest_json="{}",
        issues="no_saved_run",
        output_root="",
        artifact_root="",
    )


def audit_saved_versions(
    legacy_repo: Path,
    v18_repo: Path,
    reference_root: Path,
    *,
    workers: int = 1,
) -> AuditBundle:
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be a positive integer")
    scorer_sha256 = _sha256_file(SCORER_PATH)
    specs = discover_run_specs(legacy_repo, v18_repo)
    runs: list[RunAudit] = []
    files: list[FileAudit] = []
    cohorts: dict[str, CohortAudit] = {}
    present_suites = {(spec.version, spec.suite) for spec in specs}

    def retain(
        value: tuple[RunAudit, tuple[FileAudit, ...], CohortAudit | None],
    ) -> None:
        run, run_files, cohort = value
        runs.append(run)
        files.extend(run_files)
        if cohort is not None:
            existing = cohorts.get(cohort.cohort_id)
            if existing is not None and existing != cohort:
                raise RuntimeError("cohort SHA-256 collision or inconsistent manifest")
            cohorts[cohort.cohort_id] = cohort

    if workers == 1 or len(specs) < 2:
        for index, spec in enumerate(specs, start=1):
            retain(audit_run(spec, reference_root, scorer_sha256))
            print(
                f"saved audit {index}/{len(specs)}: "
                f"v{spec.version}/{spec.suite}/{spec.engine}",
                file=sys.stderr,
                flush=True,
            )
    else:
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=min(workers, len(specs)),
            mp_context=multiprocessing.get_context("fork"),
        ) as executor:
            pending = {
                executor.submit(
                    audit_run,
                    spec,
                    reference_root,
                    scorer_sha256,
                ): spec
                for spec in specs
            }
            for index, future in enumerate(
                concurrent.futures.as_completed(pending),
                start=1,
            ):
                spec = pending[future]
                retain(future.result())
                print(
                    f"saved audit {index}/{len(specs)}: "
                    f"v{spec.version}/{spec.suite}/{spec.engine}",
                    file=sys.stderr,
                    flush=True,
                )

    for version in VERSIONS:
        for suite in SUITES:
            if (version, suite) not in present_suites:
                runs.append(_missing_suite_run(version, suite, scorer_sha256))

    runs.sort(
        key=lambda item: (
            int(item.version[1:]),
            SUITE_ORDER[item.suite],
            ENGINE_ORDER.get(item.engine, len(ENGINES)),
        )
    )
    files.sort(
        key=lambda item: (
            int(item.version[1:]),
            SUITE_ORDER[item.suite],
            ENGINE_ORDER[item.engine],
            item.file,
        )
    )
    return AuditBundle(
        runs=tuple(runs),
        files=tuple(files),
        cohorts=tuple(sorted(cohorts.values(), key=lambda item: item.cohort_id)),
        scorer_sha256=scorer_sha256,
        reference_root=reference_root,
    )


def _write_csv(path: Path, rows: Iterable[object], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _short_cohort(value: str) -> str:
    return value[:12] if value != "n/a" else value


def render_markdown(bundle: AuditBundle) -> str:
    lines = [
        "# Saved OCR audit v1–v19",
        "",
        "This report rescored saved Markdown only; no OCR was run. "
        "Only the current reference root is authoritative.",
        "",
        f"- Current references: `{_escape(bundle.reference_root)}`",
        f"- Current scorer SHA-256: `{bundle.scorer_sha256}`",
        "- Cohorts are exact `(file, current-reference SHA-256)` sets. "
        "Scores from different cohort IDs must not be averaged or ranked together.",
        "- `error` and empty/invalid inputs are fail-closed: their aggregate metrics "
        "remain `n/a`. `partial_reference` scores only its explicitly identified "
        "current-reference cohort.",
        "- v19 is labelled as a saved rerun from the v18-offline workspace, not as "
        "an independently preserved Git branch.",
        "",
        "## Version coverage",
        "",
        "| Version | Native | Raster PNG | Images |",
        "| --- | --- | --- | --- |",
    ]
    by_version_suite: dict[tuple[str, str], list[RunAudit]] = {}
    for run in bundle.runs:
        by_version_suite.setdefault((run.version, run.suite), []).append(run)
    for version in VERSIONS:
        cells: list[str] = []
        for suite in SUITES:
            items = by_version_suite[(f"v{version}", suite)]
            if len(items) == 1 and items[0].engine == "n/a":
                cells.append("n/a (not saved)")
                continue
            statuses = ", ".join(f"{item.engine}:{item.status}" for item in items)
            cells.append(statuses)
        lines.append(f"| v{version} | {' | '.join(_escape(cell) for cell in cells)} |")

    lines.extend(
        [
            "",
            "## Cohort-aware run results",
            "",
            "| Version | Suite | Engine | Provenance | Status | Cohort | "
            "Scored / inventory | Micro | Macro | Mean time | Issues |",
            "| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for run in bundle.runs:
        rerun = f"; {run.saved_rerun_of}" if run.saved_rerun_of else ""
        lines.append(
            "| {} | `{}` | `{}` | `{}` | **{}** | `{}` | {}/{} | {} | {} | {} | {} |".format(
                run.version,
                _escape(run.suite),
                _escape(run.engine),
                _escape(f"{run.provenance}{rerun}"),
                _escape(run.status),
                _short_cohort(run.cohort_id),
                run.scored_files,
                run.inventory_files,
                run.micro_match_percent,
                run.macro_match_percent,
                run.mean_wall_seconds,
                _escape(run.issues or "—"),
            )
        )

    lines.extend(
        [
            "",
            "## Current-reference cohorts",
            "",
            "| Cohort | Files | Exact current reference identities |",
            "| --- | ---: | --- |",
        ]
    )
    for cohort in bundle.cohorts:
        manifest = json.loads(cohort.reference_manifest_json)
        identity = "; ".join(
            f"`{_escape(name)}`@`{digest[:12]}`"
            for name, digest in manifest.items()
        )
        lines.append(
            f"| `{_short_cohort(cohort.cohort_id)}` | {cohort.file_count} | {identity} |"
        )
    if not bundle.cohorts:
        lines.append("| n/a | 0 | No saved result has a current reference. |")
    return "\n".join(lines) + "\n"


def write_reports(
    bundle: AuditBundle,
    csv_path: Path,
    markdown_path: Path,
    details_csv: Path,
    cohorts_csv: Path,
) -> None:
    _write_csv(csv_path, bundle.runs, list(RunAudit.__dataclass_fields__))
    _write_csv(details_csv, bundle.files, list(FileAudit.__dataclass_fields__))
    _write_csv(cohorts_csv, bundle.cohorts, list(CohortAudit.__dataclass_fields__))
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(bundle), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    sibling_root = ROOT.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--legacy-repo",
        type=Path,
        default=sibling_root / "IttM-engine-original",
        help="Repository containing saved v1..v17 debug/tmp and debug/artifacts",
    )
    parser.add_argument(
        "--v18-repo",
        type=Path,
        default=sibling_root / "IttM-engine-v18-offline",
        help="Repository containing saved v18 and v19 layouts",
    )
    parser.add_argument(
        "--reference-root", type=Path, default=ROOT / "debug" / "reference"
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=ROOT / "debug" / "audit" / "saved-v1-v19.csv",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=ROOT / "debug" / "audit" / "saved-v1-v19.md",
    )
    parser.add_argument("--details-csv", type=Path)
    parser.add_argument("--cohorts-csv", type=Path)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 when any discovered saved run has an integrity error",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="Bounded process workers used to rescore independent saved runs",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    details_csv = args.details_csv or args.csv.with_name(
        f"{args.csv.stem}-files{args.csv.suffix}"
    )
    cohorts_csv = args.cohorts_csv or args.csv.with_name(
        f"{args.csv.stem}-cohorts{args.csv.suffix}"
    )
    bundle = audit_saved_versions(
        args.legacy_repo,
        args.v18_repo,
        args.reference_root,
        workers=args.workers,
    )
    write_reports(bundle, args.csv, args.markdown, details_csv, cohorts_csv)
    print(render_markdown(bundle), end="")
    if args.strict and any(run.status == "error" for run in bundle.runs):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
