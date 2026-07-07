import csv
import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
ORACLE_PATH = REPO_ROOT / "scripts" / "debug" / "debug_pipeline_oracle.py"


def _load_oracle_module():
    spec = importlib.util.spec_from_file_location(
        "debug_pipeline_oracle",
        ORACLE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_build_variants_deduplicates_resolved_flag_combinations():
    oracle = _load_oracle_module()

    variants = oracle.build_variants(
        engines=("tesseract",),
        backend_profiles={
            "tesseract": (
                "backend_tesseract_table_first",
                "backend_tesseract_table_slots",
            )
        },
        browser_profiles=(),
        lexical_modes=("off",),
        slot_modes=("line_merge_v1",),
    )

    assert len(variants) == 1
    assert variants[0].engine == "tesseract"
    assert "table_slot_builder:line_merge_v1" in variants[0].overrides


def test_oracle_rows_keep_all_strict_quality_dimensions(tmp_path):
    oracle = _load_oracle_module()
    variant = oracle.OracleVariant(
        name="sample-variant",
        engine="tesseract",
        profile="backend_tesseract_standard",
        overrides="lexical_correction:off;table_slot_builder:off",
        normalized_flags="normalized",
    )
    result = tmp_path / "result.csv"
    result.write_text(
        "\n".join(
            [
                "file,threshold,tesseract %,tesseract text gate,"
                "tesseract compact quality %,tesseract compact quality gate,"
                "tesseract lexical t9 %,tesseract lexical t9 gate,"
                "tesseract markdown grammar %,tesseract markdown grammar gate,"
                "tesseract markdown grammar notes,"
                "tesseract success probability %,"
                "tesseract success probability gate,tesseract failure kind,"
                "tesseract gate,tesseract profile,tesseract flags",
                "sample.png,90,98.00,pass,95.00,pass,99.00,pass,97.00,pass,"
                "rows=4/4,91.24,pass,,pass,backend_tesseract_standard,flags",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rows = oracle.oracle_rows_for_variant(variant, result)

    assert len(rows) == 1
    assert rows[0]["text_percent"] == "98.00"
    assert rows[0]["compact_quality_percent"] == "95.00"
    assert rows[0]["lexical_t9_percent"] == "99.00"
    assert rows[0]["markdown_grammar_percent"] == "97.00"
    assert rows[0]["success_probability_percent"] == "91.24"


def test_best_rows_use_success_probability_not_text_percent():
    oracle = _load_oracle_module()
    base = {field: "" for field in oracle.ORACLE_FIELDS}
    high_text = {
        **base,
        "variant": "high-text",
        "file": "sample.png",
        "engine": "tesseract",
        "text_percent": "100.00",
        "compact_quality_percent": "60.00",
        "success_probability_percent": "60.00",
    }
    high_success = {
        **base,
        "variant": "high-success",
        "file": "sample.png",
        "engine": "tesseract",
        "text_percent": "95.00",
        "compact_quality_percent": "95.00",
        "success_probability_percent": "91.00",
    }

    best = oracle.best_oracle_rows([high_text, high_success])

    assert [row["variant"] for row in best] == ["high-success"]


def test_write_plan_is_long_and_keeps_losing_variants(tmp_path):
    oracle = _load_oracle_module()
    variants = [
        oracle.OracleVariant("one", "tesseract", "p1", "a:1", "n1"),
        oracle.OracleVariant("two", "tesseract", "p2", "a:2", "n2"),
    ]
    output = tmp_path / "plan.csv"

    oracle.write_plan(output, variants)

    with output.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    assert [row["variant"] for row in rows] == ["one", "two"]


def test_split_variant_shards_runs_multiple_workers_per_engine():
    oracle = _load_oracle_module()
    variants = [
        oracle.OracleVariant(
            f"tess-{index}",
            "tesseract",
            "profile",
            "",
            f"n-{index}",
        )
        for index in range(5)
    ]
    variants.extend(
        oracle.OracleVariant(
            f"easy-{index}",
            "easyocr",
            "profile",
            "",
            f"e-{index}",
        )
        for index in range(3)
    )

    shards = oracle.split_variant_shards(
        variants,
        {"tesseract": 4, "easyocr": 2, "browser-tesseract": 3},
    )

    assert len(shards) == 6
    assert sorted(len(shard) for shard in shards) == [1, 1, 1, 1, 2, 2]
    assert all(len({variant.engine for variant in shard}) == 1 for shard in shards)


def test_result_file_names_rejects_empty_interrupted_result(tmp_path):
    oracle = _load_oracle_module()
    result = tmp_path / "result.csv"
    result.write_text("file,threshold\n", encoding="utf-8")

    assert oracle.result_file_names(result) == frozenset()

    result.write_text(
        "file,threshold\nimage.png,90\ndoc_README.png,90\n",
        encoding="utf-8",
    )
    assert oracle.result_file_names(result) == {
        "image.png",
        "doc_README.png",
    }


def test_result_file_names_from_summary_rejects_header_only_file(tmp_path):
    oracle = _load_oracle_module()
    summary = tmp_path / "summary.tsv"
    summary.write_text("commit\tengine\tfile\n", encoding="utf-8")

    assert oracle.result_file_names_from_summary(summary) == frozenset()

    summary.write_text(
        "commit\tengine\tfile\nabc\ttesseract\timage.png\n",
        encoding="utf-8",
    )
    assert oracle.result_file_names_from_summary(summary) == {"image.png"}
