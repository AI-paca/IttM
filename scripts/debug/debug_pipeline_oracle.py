#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OCR_ROOT = REPO_ROOT / "ocr"
if str(OCR_ROOT) not in sys.path:
    sys.path.insert(0, str(OCR_ROOT))

from app.pipeline_config import OCR_PIPELINE_PROFILES, resolve_pipeline_profile
from app.pipeline_flags import (
    LEXICAL_CORRECTION_MODES,
    TABLE_SLOT_BUILDER_MODES,
    apply_pipeline_flag_overrides,
    profile_flags_string,
)

REAL_ENGINES = ("tesseract", "easyocr", "browser-tesseract")
DEFAULT_ENGINE_WORKERS = {
    "tesseract": 4,
    "easyocr": 2,
    "browser-tesseract": 3,
}
DEFAULT_BROWSER_PROFILES = (
    "browser_tesseract_standard",
    "browser_tesseract_dewarp",
    "browser_tesseract_raw",
    "browser_tesseract_table_first",
    "browser_tesseract_table_slots",
    "browser_tesseract_table_slots_t9",
)
ORACLE_FIELDS = (
    "variant",
    "file",
    "engine",
    "requested_profile",
    "resolved_profile",
    "overrides",
    "threshold",
    "text_percent",
    "text_gate",
    "compact_quality_percent",
    "compact_quality_gate",
    "lexical_t9_percent",
    "lexical_t9_gate",
    "markdown_grammar_percent",
    "markdown_grammar_gate",
    "markdown_grammar_notes",
    "success_probability_percent",
    "success_probability_gate",
    "failure_kind",
    "overall_gate",
    "flags",
    "source_result",
)


@dataclass(frozen=True)
class OracleVariant:
    name: str
    engine: str
    profile: str
    overrides: str
    normalized_flags: str


_ACTIVE_PROCESSES: set[subprocess.Popen] = set()
_ACTIVE_PROCESSES_LOCK = threading.Lock()


def _slug(value: str) -> str:
    return "".join(character if character.isalnum() else "-" for character in value).strip("-")


def _variant_name(
    engine: str,
    profile: str,
    overrides: str,
    normalized_flags: str,
) -> str:
    digest = hashlib.sha1(normalized_flags.encode("utf-8")).hexdigest()[:10]
    labels = [engine, profile]
    if overrides:
        labels.extend(
            part.replace(":", "-").replace("=", "-")
            for part in overrides.split(";")
            if part
        )
    return f"{_slug('-'.join(labels))}-{digest}"


def _backend_profiles_for_engine(engine: str) -> tuple[str, ...]:
    engine_prefix = f"backend_{engine}_"
    profiles = [
        name
        for name in OCR_PIPELINE_PROFILES
        if name.startswith(engine_prefix)
        or name in {"backend_curriculum", "backend_plain_text", "backend_raw"}
    ]
    return tuple(sorted(profiles))


def _parse_engine_profile(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            "--backend-profile must have the form ENGINE=PROFILE"
        )
    engine, profile = value.split("=", 1)
    if engine not in {"tesseract", "easyocr"} or not profile:
        raise argparse.ArgumentTypeError(
            "--backend-profile engine must be tesseract or easyocr"
        )
    return engine, profile


def _parse_engine_workers(value: str) -> tuple[str, int]:
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            "--engine-workers must have the form ENGINE=COUNT"
        )
    engine, raw_count = value.split("=", 1)
    if engine not in REAL_ENGINES:
        raise argparse.ArgumentTypeError(
            f"--engine-workers engine must be one of: {', '.join(REAL_ENGINES)}"
        )
    try:
        count = int(raw_count)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--engine-workers count must be a positive integer"
        ) from exc
    if count < 1:
        raise argparse.ArgumentTypeError(
            "--engine-workers count must be a positive integer"
        )
    return engine, count


def build_variants(
    engines: tuple[str, ...],
    backend_profiles: dict[str, tuple[str, ...]],
    browser_profiles: tuple[str, ...],
    lexical_modes: tuple[str, ...],
    slot_modes: tuple[str, ...],
) -> list[OracleVariant]:
    variants: list[OracleVariant] = []
    seen: set[tuple[str, str]] = set()

    for engine in engines:
        if engine == "browser-tesseract":
            for profile in browser_profiles:
                normalized_flags = f"browser_profile:{profile}"
                key = (engine, normalized_flags)
                if key in seen:
                    continue
                seen.add(key)
                variants.append(
                    OracleVariant(
                        name=_variant_name(
                            engine,
                            profile,
                            "",
                            normalized_flags,
                        ),
                        engine=engine,
                        profile=profile,
                        overrides="",
                        normalized_flags=normalized_flags,
                    )
                )
            continue

        for profile_name in backend_profiles.get(engine, ()):
            base_profile = resolve_pipeline_profile(engine, profile_name)
            for lexical_mode in lexical_modes:
                for slot_mode in slot_modes:
                    overrides = (
                        f"lexical_correction:{lexical_mode};"
                        f"table_slot_builder:{slot_mode}"
                    )
                    profile = apply_pipeline_flag_overrides(base_profile, overrides)
                    normalized_flags = profile_flags_string(profile)
                    key = (engine, normalized_flags)
                    if key in seen:
                        continue
                    seen.add(key)
                    variants.append(
                        OracleVariant(
                            name=_variant_name(
                                engine,
                                profile_name,
                                overrides,
                                normalized_flags,
                            ),
                            engine=engine,
                            profile=profile_name,
                            overrides=overrides,
                            normalized_flags=normalized_flags,
                        )
                    )
    return variants


def _write_csv(path: Path, rows: list[dict[str, str]], fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_plan(path: Path, variants: list[OracleVariant]) -> None:
    rows = [
        {
            "variant": variant.name,
            "engine": variant.engine,
            "profile": variant.profile,
            "overrides": variant.overrides,
            "normalized_flags": variant.normalized_flags,
        }
        for variant in variants
    ]
    _write_csv(
        path,
        rows,
        ("variant", "engine", "profile", "overrides", "normalized_flags"),
    )


def _run_variant(
    variant: OracleVariant,
    *,
    source_root: Path,
    fixtures_root: Path,
    expected_root: Path,
    tmp_root: Path,
    output_root: Path,
    fixture_patterns: tuple[str, ...],
    expected_files: frozenset[str],
    gpu: str,
    timeout: int,
    force: bool,
) -> dict[str, str]:
    run_tmp = tmp_root / "runs" / variant.name
    run_output = output_root / "runs" / variant.name
    result_path = run_output / "result.csv"
    log_path = output_root / "logs" / f"{variant.name}.log"
    if (
        result_path.exists()
        and not force
        and result_file_names(result_path) >= expected_files
    ):
        return {
            "variant": variant.name,
            "engine": variant.engine,
            "status": "reused",
            "exit": "0",
            "result": str(result_path),
            "log": str(log_path),
        }

    command = [
        str(source_root / "scripts/debug/debug-image-engines-parallel.sh"),
        "--source",
        str(source_root),
        "--fixtures",
        str(fixtures_root),
        "--expected-root",
        str(expected_root),
        "--tmp-root",
        str(run_tmp),
        "--output-root",
        str(run_output),
        "--engines",
        variant.engine,
        "--gpu",
        gpu,
        "--timeout",
        str(timeout),
    ]
    for pattern in fixture_patterns:
        command.extend(("--fixture", pattern))
    if variant.engine == "browser-tesseract":
        command.extend(("--browser-profile", variant.profile))
    else:
        command.extend(
            (
                "--engine-profile",
                f"{variant.engine}={variant.profile}",
                "--engine-flags",
                f"{variant.engine}={variant.overrides}",
            )
        )

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=source_root,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        with _ACTIVE_PROCESSES_LOCK:
            _ACTIVE_PROCESSES.add(process)
        try:
            returncode = process.wait()
        finally:
            with _ACTIVE_PROCESSES_LOCK:
                _ACTIVE_PROCESSES.discard(process)
    status = "complete" if returncode == 0 else "gate_fail"
    if not result_path.exists():
        status = "runner_error"
    return {
        "variant": variant.name,
        "engine": variant.engine,
        "status": status,
        "exit": str(returncode),
        "result": str(result_path),
        "log": str(log_path),
    }


def split_variant_shards(
    variants: list[OracleVariant],
    engine_workers: dict[str, int],
) -> list[list[OracleVariant]]:
    shards: list[list[OracleVariant]] = []
    for engine in REAL_ENGINES:
        engine_variants = [
            variant for variant in variants if variant.engine == engine
        ]
        worker_count = min(
            engine_workers.get(engine, 1),
            len(engine_variants),
        )
        if worker_count <= 0:
            continue
        engine_shards = [[] for _ in range(worker_count)]
        for index, variant in enumerate(engine_variants):
            engine_shards[index % worker_count].append(variant)
        shards.extend(engine_shards)
    return shards


def _terminate_active_processes() -> None:
    with _ACTIVE_PROCESSES_LOCK:
        processes = list(_ACTIVE_PROCESSES)
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    for process in processes:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def execute_variants(
    variants: list[OracleVariant],
    *,
    jobs: int,
    engine_workers: dict[str, int],
    **kwargs,
) -> list[dict[str, str]]:
    shards = split_variant_shards(variants, engine_workers)

    def run_shard(shard: list[OracleVariant]) -> list[dict[str, str]]:
        return [_run_variant(variant, **kwargs) for variant in shard]

    statuses: list[dict[str, str]] = []
    executor = ThreadPoolExecutor(max_workers=min(jobs, len(shards)))
    try:
        for shard_rows in executor.map(run_shard, shards):
            statuses.extend(shard_rows)
    except KeyboardInterrupt:
        _terminate_active_processes()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    statuses.sort(key=lambda row: (row["engine"], row["variant"]))
    return statuses


def selected_fixture_names(
    fixtures_root: Path,
    fixture_patterns: tuple[str, ...],
) -> frozenset[str]:
    supported_suffixes = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
    names = {
        path.name
        for path in fixtures_root.iterdir()
        if path.is_file() and path.suffix.casefold() in supported_suffixes
    }
    if not fixture_patterns:
        return frozenset(names)
    return frozenset(
        name
        for name in names
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in fixture_patterns)
    )


def result_file_names(path: Path) -> frozenset[str]:
    if not path.exists():
        return frozenset()
    try:
        with path.open(encoding="utf-8", newline="") as source:
            return frozenset(
                row["file"]
                for row in csv.DictReader(source)
                if row.get("file")
            )
    except (OSError, csv.Error, KeyError):
        return frozenset()


def _metric(row: dict[str, str], engine: str, suffix: str) -> str:
    return row.get(f"{engine} {suffix}", "n/a")


def oracle_rows_for_variant(
    variant: OracleVariant,
    result_path: Path,
) -> list[dict[str, str]]:
    if not result_path.exists():
        return []
    with result_path.open(encoding="utf-8", newline="") as source:
        source_rows = list(csv.DictReader(source))
    rows: list[dict[str, str]] = []
    engine = variant.engine
    for row in source_rows:
        rows.append(
            {
                "variant": variant.name,
                "file": row["file"],
                "engine": engine,
                "requested_profile": variant.profile,
                "resolved_profile": row.get(f"{engine} profile", ""),
                "overrides": variant.overrides,
                "threshold": row.get("threshold", "90"),
                "text_percent": _metric(row, engine, "%"),
                "text_gate": _metric(row, engine, "text gate"),
                "compact_quality_percent": _metric(
                    row, engine, "compact quality %"
                ),
                "compact_quality_gate": _metric(
                    row, engine, "compact quality gate"
                ),
                "lexical_t9_percent": _metric(row, engine, "lexical t9 %"),
                "lexical_t9_gate": _metric(row, engine, "lexical t9 gate"),
                "markdown_grammar_percent": _metric(
                    row, engine, "markdown grammar %"
                ),
                "markdown_grammar_gate": _metric(
                    row, engine, "markdown grammar gate"
                ),
                "markdown_grammar_notes": _metric(
                    row, engine, "markdown grammar notes"
                ),
                "success_probability_percent": _metric(
                    row, engine, "success probability %"
                ),
                "success_probability_gate": _metric(
                    row, engine, "success probability gate"
                ),
                "failure_kind": _metric(row, engine, "failure kind"),
                "overall_gate": _metric(row, engine, "gate"),
                "flags": row.get(f"{engine} flags", variant.normalized_flags),
                "source_result": str(result_path),
            }
        )
    return rows


def collect_oracle_rows(
    variants: list[OracleVariant],
    output_root: Path,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for variant in variants:
        rows.extend(
            oracle_rows_for_variant(
                variant,
                output_root / "runs" / variant.name / "result.csv",
            )
        )
    rows.sort(key=lambda row: (row["file"], row["engine"], row["variant"]))
    return rows


def _rescore_variant(
    variant: OracleVariant,
    *,
    source_root: Path,
    expected_root: Path,
    tmp_root: Path,
    output_root: Path,
    expected_files: frozenset[str],
) -> dict[str, str]:
    run_tmp = tmp_root / "runs" / variant.name
    run_output = output_root / "runs" / variant.name
    merged_root = run_tmp / "merged-backend"
    browser_root = run_tmp / "browser-tesseract"
    log_path = output_root / "rescore-logs" / f"{variant.name}.log"
    result_path = run_output / "result.csv"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    commands: list[list[str]] = []

    backend_summary = merged_root / "summary.tsv"
    if result_file_names_from_summary(backend_summary):
        commands.append(
            [
                sys.executable,
                str(source_root / "scripts/debug/debug_report.py"),
                "--summary",
                str(backend_summary),
                "--output-root",
                str(merged_root),
                "--expected-root",
                str(expected_root),
                "--markdown",
                str(merged_root / "comparison.md"),
                "--tables-root",
                str(merged_root / "tables"),
                "--csv",
                str(merged_root / "comparison.csv"),
            ]
        )

    matrix_command = [
        sys.executable,
        str(source_root / "scripts/debug/debug_matrix_report.py"),
        "--benchmark-root",
        str(merged_root),
        "--expected-root",
        str(expected_root),
        "--output-root",
        str(run_output),
        "--include-auto",
    ]
    if result_file_names_from_summary(browser_root / "summary.tsv"):
        matrix_command.extend(("--browser-root", str(browser_root)))
    commands.append(matrix_command)

    returncode = 0
    with log_path.open("w", encoding="utf-8") as log:
        for command in commands:
            completed = subprocess.run(
                command,
                cwd=source_root,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
                text=True,
            )
            if completed.returncode != 0:
                returncode = completed.returncode
                break
    complete = result_file_names(result_path) >= expected_files
    return {
        "variant": variant.name,
        "engine": variant.engine,
        "status": "rescored" if returncode == 0 and complete else "rescore_error",
        "exit": str(returncode),
        "result": str(result_path),
        "log": str(log_path),
    }


def result_file_names_from_summary(path: Path) -> frozenset[str]:
    if not path.exists():
        return frozenset()
    try:
        with path.open(encoding="utf-8", newline="") as source:
            return frozenset(
                row["file"]
                for row in csv.DictReader(source, delimiter="\t")
                if row.get("file")
            )
    except (OSError, csv.Error, KeyError):
        return frozenset()


def rescore_variants(
    variants: list[OracleVariant],
    *,
    jobs: int,
    **kwargs,
) -> list[dict[str, str]]:
    with ThreadPoolExecutor(max_workers=min(jobs, len(variants))) as executor:
        statuses = list(
            executor.map(
                lambda variant: _rescore_variant(variant, **kwargs),
                variants,
            )
        )
    statuses.sort(key=lambda row: (row["engine"], row["variant"]))
    return statuses


def _percent(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0


def best_oracle_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    best: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = (row["file"], row["engine"])
        score = (
            _percent(row["success_probability_percent"]),
            _percent(row["compact_quality_percent"]),
            _percent(row["text_percent"]),
        )
        previous = best.get(key)
        if previous is None:
            best[key] = row
            continue
        previous_score = (
            _percent(previous["success_probability_percent"]),
            _percent(previous["compact_quality_percent"]),
            _percent(previous["text_percent"]),
        )
        if score > previous_score:
            best[key] = row
    return [best[key] for key in sorted(best)]


def _write_summary(path: Path, best_rows: list[dict[str, str]]) -> None:
    lines = [
        "# Pipeline Oracle Best Results",
        "",
        "`best.csv` is derived from `oracle.csv`; losing combinations remain in the latter.",
        "",
        "| File | Engine | Success | Text | Compact | Lexical | Grammar | Profile | Overrides |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in best_rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{row['file']}`",
                    f"`{row['engine']}`",
                    row["success_probability_percent"],
                    row["text_percent"],
                    row["compact_quality_percent"],
                    row["lexical_t9_percent"],
                    row["markdown_grammar_percent"],
                    f"`{row['resolved_profile'] or row['requested_profile']}`",
                    f"`{row['overrides'] or '-'}`",
                ]
            )
            + " |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Exhaustively run real OCR profile/flag combinations and preserve "
            "all strict quality dimensions in a long oracle CSV."
        )
    )
    parser.add_argument("--source", default=REPO_ROOT, type=Path)
    parser.add_argument("--fixtures", default=Path("debug/fixtures"), type=Path)
    parser.add_argument(
        "--expected-root", default=Path("debug/reference"), type=Path
    )
    parser.add_argument(
        "--tmp-root", default=Path("debug/tmp/pipeline-oracle"), type=Path
    )
    parser.add_argument(
        "--output-root",
        default=Path("debug/artifacts/pipeline-oracle"),
        type=Path,
    )
    parser.add_argument(
        "--engines",
        default=",".join(REAL_ENGINES),
        help="Comma-separated real engines; auto is intentionally unsupported.",
    )
    parser.add_argument("--fixture", action="append", dest="fixtures_filter")
    parser.add_argument(
        "--backend-profile",
        action="append",
        type=_parse_engine_profile,
        default=[],
        help="Restrict backend profiles with ENGINE=PROFILE; repeatable.",
    )
    parser.add_argument(
        "--browser-profile",
        action="append",
        default=[],
        help="Restrict browser profiles; repeatable.",
    )
    parser.add_argument(
        "--lexical-mode",
        action="append",
        choices=sorted(LEXICAL_CORRECTION_MODES),
        dest="lexical_modes",
    )
    parser.add_argument(
        "--slot-mode",
        action="append",
        choices=sorted(TABLE_SLOT_BUILDER_MODES),
        dest="slot_modes",
    )
    parser.add_argument(
        "--engine-workers",
        action="append",
        type=_parse_engine_workers,
        default=[],
        help=(
            "Concurrent independent workers for one engine, e.g. "
            "tesseract=4. Each backend worker gets its own Docker port."
        ),
    )
    parser.add_argument("--jobs", type=int, default=sum(DEFAULT_ENGINE_WORKERS.values()))
    parser.add_argument("--gpu", choices=("auto", "on", "off"), default="auto")
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--max-variants-per-engine", type=int)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument(
        "--rescore-only",
        action="store_true",
        help="Rebuild strict result/oracle CSV files from existing Markdown without OCR.",
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    engines = tuple(
        engine.strip() for engine in args.engines.split(",") if engine.strip()
    )
    unknown = sorted(set(engines) - set(REAL_ENGINES))
    if unknown:
        raise SystemExit(f"Unknown or non-real engines: {', '.join(unknown)}")
    if args.jobs < 1:
        raise SystemExit("--jobs must be positive")
    engine_workers = dict(DEFAULT_ENGINE_WORKERS)
    engine_workers.update(args.engine_workers)

    backend_profiles: dict[str, tuple[str, ...]] = {}
    explicit_profiles: dict[str, list[str]] = {}
    for engine, profile in args.backend_profile:
        explicit_profiles.setdefault(engine, []).append(profile)
    for engine in ("tesseract", "easyocr"):
        backend_profiles[engine] = tuple(
            explicit_profiles.get(engine) or _backend_profiles_for_engine(engine)
        )

    variants = build_variants(
        engines=engines,
        backend_profiles=backend_profiles,
        browser_profiles=tuple(
            args.browser_profile or DEFAULT_BROWSER_PROFILES
        ),
        lexical_modes=tuple(
            args.lexical_modes or sorted(LEXICAL_CORRECTION_MODES)
        ),
        slot_modes=tuple(
            args.slot_modes or sorted(TABLE_SLOT_BUILDER_MODES)
        ),
    )
    if args.max_variants_per_engine is not None:
        limited: list[OracleVariant] = []
        for engine in REAL_ENGINES:
            limited.extend(
                [
                    variant
                    for variant in variants
                    if variant.engine == engine
                ][: args.max_variants_per_engine]
            )
        variants = limited

    source_root = args.source.resolve()
    fixtures_root = (source_root / args.fixtures).resolve()
    expected_root = (source_root / args.expected_root).resolve()
    tmp_root = (source_root / args.tmp_root).resolve()
    output_root = (source_root / args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    write_plan(output_root / "plan.csv", variants)
    if args.plan_only:
        print(f"Wrote {len(variants)} variants to {output_root / 'plan.csv'}")
        return 0

    expected_files = selected_fixture_names(
        fixtures_root,
        tuple(args.fixtures_filter or ()),
    )
    if args.rescore_only:
        statuses = rescore_variants(
            variants,
            jobs=args.jobs,
            source_root=source_root,
            expected_root=expected_root,
            tmp_root=tmp_root,
            output_root=output_root,
            expected_files=expected_files,
        )
    else:
        statuses = execute_variants(
            variants,
            jobs=args.jobs,
            engine_workers=engine_workers,
            source_root=source_root,
            fixtures_root=fixtures_root,
            expected_root=expected_root,
            tmp_root=tmp_root,
            output_root=output_root,
            fixture_patterns=tuple(args.fixtures_filter or ()),
            expected_files=expected_files,
            gpu=args.gpu,
            timeout=args.timeout,
            force=args.force,
        )
    _write_csv(
        output_root / "run-status.csv",
        statuses,
        ("variant", "engine", "status", "exit", "result", "log"),
    )
    oracle_rows = collect_oracle_rows(variants, output_root)
    _write_csv(output_root / "oracle.csv", oracle_rows, ORACLE_FIELDS)
    best_rows = best_oracle_rows(oracle_rows)
    _write_csv(output_root / "best.csv", best_rows, ORACLE_FIELDS)
    _write_summary(output_root / "summary.md", best_rows)

    runner_errors = [
        row
        for row in statuses
        if row["status"] in {"runner_error", "rescore_error"}
    ]
    print(
        f"Wrote {len(oracle_rows)} oracle rows and {len(best_rows)} best rows "
        f"to {output_root}"
    )
    return 1 if runner_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
