import csv
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = REPO_ROOT / "scripts" / "debug_matrix_report.py"


def _load_report_module():
    spec = importlib.util.spec_from_file_location("debug_matrix_report", REPORT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_debug_matrix_report_writes_per_method_csv(tmp_path):
    benchmark_root = tmp_path / "benchmark"
    benchmark_root.mkdir()
    (benchmark_root / "comparison.csv").write_text(
        "\n".join(
            [
                "file,method,wall_seconds,match_percent,matched_expected_lines,total_expected_lines,table_markdown_files",
                "sample.png,auto,0.100,100.00,10,10,0",
                "sample.png,tesseract,1.250,90.00,9,10,0",
                "sample.png,easyocr,2.500,95.00,9,10,0",
                '"Adobe Scan Oct 26, 2022 (1).pdf.page-001.raster.png",tesseract,3.000,71.00,71,100,0',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (benchmark_root / "summary.tsv").write_text(
        "\n".join(
            [
                "commit\tengine\tpipeline\tfile\thttp_status\tcurl_exit\twall_ms\tbackend_elapsed_ms\tpages\tchunks\ttables_found\ttable_cells",
                "abc\tauto\tbackend_auto_standard\tsample.png\t200\t0\t100\t90\t1\t1\t0\t0",
                "abc\ttesseract\tbackend_tesseract_standard\tsample.png\t200\t0\t1250\t1200\t1\t1\t0\t0",
                "abc\teasyocr\tbackend_easyocr_standard\tsample.png\t200\t0\t2500\t2400\t1\t1\t1\t4",
                "abc\ttesseract\tbackend_tesseract_standard\tAdobe Scan Oct 26, 2022 (1).pdf.page-001.raster.png\t200\t0\t3000\t2900\t1\t1\t0\t0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (benchmark_root / "manifest.md").write_text(
        "- command: `scripts/run-debug.sh --engines tesseract,easyocr`\n",
        encoding="utf-8",
    )
    expected_root = tmp_path / "expected"
    expected_root.mkdir()
    (expected_root / "sample.png.md").write_text("Useful browser text\n", encoding="utf-8")
    browser_root = tmp_path / "browser"
    browser_root.mkdir()
    (browser_root / "summary.tsv").write_text(
        "\n".join(
            [
                "commit\tfile\texit\twall_ms\tengine_elapsed_ms\trss_before_bytes\trss_after_bytes\tprofile\tflags",
                "abc\tsample.png\t0\t750\t700\t1\t2\tbrowser_tesseract_dewarp\tocr_runtime:tesseract.js; ocr_languages:rus+eng+chi_sim; preprocess:projector_slide_dewarp",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (browser_root / "sample.png.md").write_text(
        "# 750 ms (engine: 700 ms, exit 0)\n---\nUseful browser text\n",
        encoding="utf-8",
    )

    output_root = tmp_path / "results"
    report = _load_report_module()
    assert (
        report.main(
            [
                "--benchmark-root",
                str(benchmark_root),
                "--browser-root",
                str(browser_root),
                "--expected-root",
                str(expected_root),
                "--output-root",
                str(output_root),
            ]
        )
        == 0
    )

    with (output_root / "result.csv").open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))

    assert "auto %" not in rows[0]
    assert "best_method" not in rows[0]
    rows_by_file = {row["file"]: row for row in rows}
    sample = rows_by_file["sample.png"]
    raster = rows_by_file["Adobe Scan Oct 26, 2022 (1).pdf.page-001.raster.png"]
    assert sample["browser-tesseract %"] == "100.00"
    assert sample["tesseract gate"] == "pass"
    assert sample["easyocr gate"] == "pass"
    assert sample["browser-tesseract gate"] == "pass"
    assert sample["browser-tesseract profile"] == "browser_tesseract_dewarp"
    assert "ocr_language_priority:rus+eng+kaz+kir+chi_sim" in sample["tesseract flags"]
    assert "ocr_table_word_psm:6" in sample["tesseract flags"]
    assert "ocr_large_table_word_psm:11" in sample["tesseract flags"]
    assert "table_raw_text_fallback:True" in sample["tesseract flags"]
    assert "sparse_text_fallback_engine:tesseract" in sample["easyocr flags"]
    assert (
        sample["browser-tesseract flags"]
        == "ocr_runtime:tesseract.js; ocr_languages:rus+eng+chi_sim; preprocess:projector_slide_dewarp"
    )
    assert "preprocess:projected_document_dewarp" not in sample["browser-tesseract flags"]
    assert raster["threshold"] == "70"
    assert raster["tesseract gate"] == "pass"
    assert (output_root / "time.csv").exists()
    assert not (output_root / "result.xlsx").exists()
