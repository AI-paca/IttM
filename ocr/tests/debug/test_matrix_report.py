import csv
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
REPORT_PATH = REPO_ROOT / "scripts" / "debug" / "debug_matrix_report.py"


def _load_report_module():
    spec = importlib.util.spec_from_file_location("debug_matrix_report", REPORT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_writes_per_method_csv(tmp_path):
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
        "- command: `scripts/debug/run-debug.sh --engines tesseract,easyocr`\n",
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
    assert raster["threshold"] == "90"
    assert raster["tesseract gate"] == "fail"
    assert (output_root / "time.csv").exists()
    assert not (output_root / "result.xlsx").exists()


def test_aggregate_raster_reference_scores_ordered_pages_for_all_methods(
    tmp_path,
):
    benchmark_root = tmp_path / "benchmark"
    fixtures = benchmark_root / "fixtures"
    fixtures.mkdir(parents=True)
    methods = ("tesseract", "easyocr")
    pages = (
        "plan.pdf.page-001.raster.png",
        "plan.pdf.page-002.raster.png",
    )
    comparison_lines = [
        "file,method,wall_seconds,match_percent",
    ]
    summary_lines = [
        "commit\tengine\tpipeline\tfile\thttp_status\tcurl_exit\twall_ms\tflags",
    ]
    for method in methods:
        (benchmark_root / method).mkdir()
        for page_number, page_name in enumerate(pages, start=1):
            (fixtures / page_name).write_bytes(b"png")
            (benchmark_root / method / f"{page_name}.md").write_text(
                f"page {page_number}\n",
                encoding="utf-8",
            )
            comparison_lines.append(f"{page_name},{method},1.000,n/a")
            summary_lines.append(
                f"abc\t{method}\tprofile\t{page_name}\t200\t0\t1000\tflag"
            )
    (benchmark_root / "comparison.csv").write_text(
        "\n".join(comparison_lines) + "\n",
        encoding="utf-8",
    )
    (benchmark_root / "summary.tsv").write_text(
        "\n".join(summary_lines) + "\n",
        encoding="utf-8",
    )
    expected_root = tmp_path / "expected"
    expected_root.mkdir()
    (expected_root / "plan.pdf.raster.png.md").write_text(
        "page 1\n\npage 2\n",
        encoding="utf-8",
    )
    browser_root = tmp_path / "browser"
    browser_root.mkdir()
    browser_summary = [
        "commit\tfile\texit\twall_ms\tprofile\tflags",
    ]
    for page_number, page_name in enumerate(pages, start=1):
        (browser_root / f"{page_name}.md").write_text(
            f"page {page_number}\n",
            encoding="utf-8",
        )
        browser_summary.append(
            f"abc\t{page_name}\t0\t500\tbrowser_profile\tbrowser_flag"
        )
    (browser_root / "summary.tsv").write_text(
        "\n".join(browser_summary) + "\n",
        encoding="utf-8",
    )

    report = _load_report_module()
    header, rows, _, _ = report.build_tables(
        benchmark_root,
        expected_root=expected_root,
        browser_root=browser_root,
    )

    assert len(rows) == 1
    result = dict(zip(header, rows[0]))
    assert result["file"] == "plan.pdf.raster.png"
    assert result["tesseract %"] == "100.00"
    assert result["easyocr %"] == "100.00"
    assert result["browser-tesseract %"] == "100.00"
    assert result["tesseract gate"] == "pass"
    assert (
        benchmark_root / "tesseract" / "plan.pdf.raster.png.md"
    ).read_text(encoding="utf-8") == "page 1\n\npage 2\n"


def test_aggregate_raster_reference_reports_missing_page_partial(tmp_path):
    benchmark_root = tmp_path / "benchmark"
    fixtures = benchmark_root / "fixtures"
    fixtures.mkdir(parents=True)
    pages = (
        "plan.pdf.page-001.raster.png",
        "plan.pdf.page-002.raster.png",
    )
    for page_name in pages:
        (fixtures / page_name).write_bytes(b"png")
    output = benchmark_root / "tesseract"
    output.mkdir()
    (output / f"{pages[0]}.md").write_text("page 1\n", encoding="utf-8")
    (benchmark_root / "comparison.csv").write_text(
        "file,method,wall_seconds,match_percent\n"
        f"{pages[0]},tesseract,1.000,n/a\n"
        f"{pages[1]},tesseract,2.000,n/a\n",
        encoding="utf-8",
    )
    (benchmark_root / "summary.tsv").write_text(
        "commit\tengine\tpipeline\tfile\thttp_status\tcurl_exit\twall_ms\tflags\n"
        f"abc\ttesseract\tprofile\t{pages[0]}\t200\t0\t1000\tflag\n"
        f"abc\ttesseract\tprofile\t{pages[1]}\t000\t28\t2000\tflag\n",
        encoding="utf-8",
    )
    expected_root = tmp_path / "expected"
    expected_root.mkdir()
    (expected_root / "plan.pdf.raster.png.md").write_text(
        "page 1\n\npage 2\n",
        encoding="utf-8",
    )

    report = _load_report_module()
    header, rows, _, _ = report.build_tables(
        benchmark_root,
        expected_root=expected_root,
    )

    result = dict(zip(header, rows[0]))
    assert result["file"] == "plan.pdf.raster.png"
    assert result["tesseract gate"] == "not_checked"
    assert "page-002(curl_exit=28)" in result["tesseract failure kind"]
    status = output / "plan.pdf.raster.png.md.aggregate-status.txt"
    assert "status=partial" in status.read_text(encoding="utf-8")
    assert "page-002(curl_exit=28)" in status.read_text(encoding="utf-8")


def test_exact_page_reference_disables_document_aggregate(tmp_path):
    benchmark_root = tmp_path / "benchmark"
    fixtures = benchmark_root / "fixtures"
    fixtures.mkdir(parents=True)
    page_name = "plan.pdf.page-001.raster.png"
    (fixtures / page_name).write_bytes(b"png")
    (benchmark_root / "tesseract").mkdir()
    (benchmark_root / "tesseract" / f"{page_name}.md").write_text(
        "page 1\n",
        encoding="utf-8",
    )
    (benchmark_root / "comparison.csv").write_text(
        "file,method,wall_seconds,match_percent\n"
        f"{page_name},tesseract,1.000,100.00\n",
        encoding="utf-8",
    )
    (benchmark_root / "summary.tsv").write_text(
        "commit\tengine\tpipeline\tfile\thttp_status\tcurl_exit\twall_ms\tflags\n"
        f"abc\ttesseract\tprofile\t{page_name}\t200\t0\t1000\tflag\n",
        encoding="utf-8",
    )
    expected_root = tmp_path / "expected"
    expected_root.mkdir()
    (expected_root / f"{page_name}.md").write_text(
        "page 1\n",
        encoding="utf-8",
    )
    (expected_root / "plan.pdf.raster.png.md").write_text(
        "aggregate truth\n",
        encoding="utf-8",
    )

    report = _load_report_module()
    header, rows, _, _ = report.build_tables(
        benchmark_root,
        expected_root=expected_root,
    )

    result = dict(zip(header, rows[0]))
    assert result["file"] == page_name
    assert result["tesseract %"] == "100.00"
