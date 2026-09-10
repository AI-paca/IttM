from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def audit_module() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts" / "debug" / "audit_saved_versions.py"
    name = "_audit_saved_versions_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write_result(path: Path, engine: str, names: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("file", f"{engine} %"))
        writer.writerows((name, "discarded-old-score") for name in names)


def _write_summary(
    path: Path,
    engine: str,
    rows: tuple[tuple[str, str, str, str], ...],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(("engine", "file", "http_status", "curl_exit", "wall_ms"))
        for name, http_status, curl_exit, wall_ms in rows:
            writer.writerow((engine, name, http_status, curl_exit, wall_ms))


def _write_actual(root: Path, engine: str, name: str, text: str) -> None:
    target = root / engine / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _legacy_run(
    legacy_repo: Path,
    *,
    version: int,
    suite: str,
    engine: str,
    names: tuple[str, ...],
) -> tuple[Path, Path]:
    output = legacy_repo / "debug" / "tmp" / f"{suite}-v{version}"
    artifact = legacy_repo / "debug" / "artifacts" / f"{suite}-v{version}"
    _write_result(artifact / "result.csv", engine, names)
    _write_summary(
        output / "summary.tsv",
        engine,
        tuple((name, "200", "0", "1000") for name in names),
    )
    return output, artifact


def test_cohort_identity_uses_exact_files_and_current_reference_digests(
    audit_module: ModuleType,
) -> None:
    first = {"b.png": "2" * 64, "a.png": "1" * 64}
    reordered = {"a.png": "1" * 64, "b.png": "2" * 64}
    changed = {"a.png": "1" * 64, "b.png": "3" * 64}

    first_id, first_files, first_manifest = audit_module._cohort_identity(first)
    second_id, _, _ = audit_module._cohort_identity(reordered)
    changed_id, _, _ = audit_module._cohort_identity(changed)

    assert first_id == second_id
    assert first_id != changed_id
    assert first_files == '["a.png","b.png"]'
    assert first_manifest.index("a.png") < first_manifest.index("b.png")


def test_legacy_audit_scores_only_current_reference_and_reports_partial_cohort(
    audit_module: ModuleType, tmp_path: Path
) -> None:
    legacy = tmp_path / "legacy"
    v18 = tmp_path / "v18"
    references = tmp_path / "current-reference"
    references.mkdir()
    (references / "kept.png.md").write_text("Alpha\n", encoding="utf-8")
    output, _ = _legacy_run(
        legacy,
        version=1,
        suite="image-fixtures",
        engine="tesseract",
        names=("kept.png", "missing.png"),
    )
    _write_actual(output, "tesseract", "kept.png", "Alpha\n")
    _write_actual(output, "tesseract", "missing.png", "Wrong\n")
    generated_reference = output / "combined-reference" / "missing.png.md"
    generated_reference.parent.mkdir()
    generated_reference.write_text("Wrong\n", encoding="utf-8")

    bundle = audit_module.audit_saved_versions(legacy, v18, references)
    run = next(
        row
        for row in bundle.runs
        if row.version == "v1" and row.suite == "image-fixtures" and row.engine == "tesseract"
    )

    assert run.status == "partial_reference"
    assert run.inventory_files == 2
    assert run.scored_files == 1
    assert run.missing_current_references == 1
    assert run.micro_match_percent == "100.000000"
    assert run.macro_match_percent == "100.000000"
    assert list(__import__("json").loads(run.reference_manifest_json)) == ["kept.png"]
    missing = next(row for row in bundle.files if row.file == "missing.png")
    assert missing.status == "n/a"
    assert missing.reason == "missing_current_reference"
    assert all(f"v{version}" in {row.version for row in bundle.runs} for version in range(1, 20))


def test_v18_split_layout_and_v19_saved_rerun_share_only_exact_cohort(audit_module: ModuleType, tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    repo = tmp_path / "v18-repo"
    references = tmp_path / "reference"
    references.mkdir()
    (references / "sample.png.md").write_text("Sample\n", encoding="utf-8")
    suite = "full-pdf-as-png"
    engine = "tesseract"

    output18 = repo / "debug" / "tmp" / f"{suite}-v18-{engine}"
    artifact18 = repo / "debug" / "artifacts" / f"{suite}-v18-{engine}"
    output19 = repo / "debug" / "tmp" / f"{suite}-par-{engine}"
    artifact19 = repo / "debug" / "artifacts" / "v19" / engine / suite
    for output, artifact in ((output18, artifact18), (output19, artifact19)):
        _write_result(artifact / "result.csv", engine, ("sample.png",))
        _write_summary(
            output / "summary.tsv",
            engine,
            (("sample.png", "200", "0", "500"),),
        )
        _write_actual(output, engine, "sample.png", "Sample\n")

    specs = audit_module.discover_run_specs(legacy, repo)
    spec18 = next(row for row in specs if row.version == 18)
    spec19 = next(row for row in specs if row.version == 19)
    assert spec18.provenance == "saved_v18_split_engine"
    assert spec19.provenance == "saved_rerun_from_v18_offline"
    assert spec19.saved_rerun_of == "v18-identical-published-csv"

    bundle = audit_module.audit_saved_versions(legacy, repo, references)
    run18 = next(row for row in bundle.runs if row.version == "v18" and row.engine == engine)
    run19 = next(row for row in bundle.runs if row.version == "v19" and row.engine == engine)
    assert run18.status == run19.status == "ok"
    assert run18.cohort_id == run19.cohort_id
    assert run19.saved_rerun_of == "v18-identical-published-csv"


def test_missing_output_and_failed_execution_are_fail_closed_not_averaged(
    audit_module: ModuleType, tmp_path: Path
) -> None:
    legacy = tmp_path / "legacy"
    references = tmp_path / "reference"
    references.mkdir()
    for name in ("good.png", "failed.png"):
        (references / f"{name}.md").write_text("Expected\n", encoding="utf-8")
    output, _ = _legacy_run(
        legacy,
        version=2,
        suite="image-fixtures",
        engine="tesseract",
        names=("good.png", "failed.png"),
    )
    _write_actual(output, "tesseract", "good.png", "Expected\n")
    _write_summary(
        output / "summary.tsv",
        "tesseract",
        (
            ("good.png", "200", "0", "100"),
            ("failed.png", "400", "0", "100"),
        ),
    )

    bundle = audit_module.audit_saved_versions(legacy, tmp_path / "v18", references)
    run = next(row for row in bundle.runs if row.version == "v2" and row.engine == "tesseract")

    assert run.status == "error"
    assert run.missing_outputs == 1
    assert run.failed_saved_rows == 1
    assert run.scored_files == 1
    assert run.micro_match_percent == "n/a"
    assert run.macro_match_percent == "n/a"
    failed = next(row for row in bundle.files if row.file == "failed.png")
    assert failed.status == "error"
    assert failed.match_percent == "n/a"


def test_reports_emit_summary_detail_cohort_csv_and_v19_warning(audit_module: ModuleType, tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    references = tmp_path / "reference"
    references.mkdir()
    (references / "one.png.md").write_text("One\n", encoding="utf-8")
    output, _ = _legacy_run(
        legacy,
        version=17,
        suite="image-fixtures",
        engine="tesseract",
        names=("one.png",),
    )
    _write_actual(output, "tesseract", "one.png", "One\n")
    bundle = audit_module.audit_saved_versions(legacy, tmp_path / "v18", references)
    summary = tmp_path / "report.csv"
    details = tmp_path / "report-files.csv"
    cohorts = tmp_path / "report-cohorts.csv"
    markdown = tmp_path / "report.md"

    audit_module.write_reports(bundle, summary, markdown, details, cohorts)

    with summary.open(encoding="utf-8", newline="") as handle:
        summary_rows = list(csv.DictReader(handle))
    with details.open(encoding="utf-8", newline="") as handle:
        detail_rows = list(csv.DictReader(handle))
    with cohorts.open(encoding="utf-8", newline="") as handle:
        cohort_rows = list(csv.DictReader(handle))
    report = markdown.read_text(encoding="utf-8")
    assert any(row["version"] == "v17" for row in summary_rows)
    assert detail_rows[0]["reference_sha256"]
    assert cohort_rows[0]["cohort_id"]
    assert "saved rerun from the v18-offline workspace" in report
    assert "Scores from different cohort IDs must not be averaged" in report
