import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE_PATH = REPO_ROOT / "scripts" / "debug" / "debug_quality_gate.py"


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("debug_quality_gate", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_fails_only_real_failures(tmp_path, capsys):
    result = tmp_path / "result.csv"
    result.write_text(
        "\n".join(
            [
                "file,threshold,tesseract %,browser-tesseract %,tesseract gate,browser-tesseract gate",
                "sample.pdf,87,90.00,n/a,pass,n/a",
                "bad.png,87,40.00,n/a,fail,n/a",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    gate = _load_gate_module()

    assert gate.main(["--result", str(result)]) == 1
    assert "bad.png: tesseract=40.00% < 87%" in capsys.readouterr().out


def test_can_treat_na_as_failure(tmp_path):
    result = tmp_path / "result.csv"
    result.write_text(
        "\n".join(
            [
                "file,threshold,browser-tesseract %,browser-tesseract gate",
                "sample.png,87,n/a,n/a",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    gate = _load_gate_module()

    assert gate.main(["--result", str(result)]) == 0
    assert gate.main(["--result", str(result), "--strict-na"]) == 1


def test_text_gate_failure_uses_method_percent_not_missing_metric(tmp_path, capsys):
    result = tmp_path / "result.csv"
    result.write_text(
        "\n".join(
            [
                "file,threshold,browser-tesseract %,browser-tesseract text gate,browser-tesseract gate",
                "sample.png,90,84.62,fail,fail",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    gate = _load_gate_module()

    assert gate.main(["--result", str(result)]) == 1
    output = capsys.readouterr().out
    assert "sample.png: browser-tesseract text=84.62% < 90%" in output
    assert "n/a%" not in output


def test_method_gate_failure_uses_success_probability_when_available(tmp_path, capsys):
    result = tmp_path / "result.csv"
    result.write_text(
        "\n".join(
            [
                "file,threshold,tesseract %,tesseract success probability %,tesseract gate",
                "doc_README.png,90,98.46,57.93,fail",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    gate = _load_gate_module()

    assert gate.main(["--result", str(result)]) == 1
    output = capsys.readouterr().out
    assert "doc_README.png: tesseract overall=57.93% < 90%" in output
    assert "doc_README.png: tesseract=98.46% < 90%" not in output


def test_required_methods_fail_when_column_is_missing(tmp_path, capsys):
    result = tmp_path / "result.csv"
    result.write_text(
        "\n".join(
            [
                "file,threshold,tesseract %,tesseract gate",
                "sample.png,90,95.00,pass",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    gate = _load_gate_module()

    assert (
        gate.main(
            [
                "--result",
                str(result),
                "--required-methods",
                "tesseract,easyocr,browser-tesseract",
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "missing required method column: easyocr" in output
    assert "missing required method column: browser-tesseract" in output


def test_incomplete_scoring_is_explicit_partial_by_default(tmp_path, capsys):
    result = tmp_path / "result.csv"
    result.write_text(
        "file,threshold,tesseract %,tesseract gate\n"
        "sample.pdf.page-001.raster.png,90,missing_reference,missing_reference\n",
        encoding="utf-8",
    )
    gate = _load_gate_module()

    assert gate.main(["--result", str(result)]) == 0
    output = capsys.readouterr().out
    assert "Debug quality gate PARTIAL:" in output
    assert "tesseract=missing_reference" in output
    assert "passed" not in output


def test_required_complete_scoring_fails_missing_and_not_checked(
    tmp_path,
    capsys,
):
    result = tmp_path / "result.csv"
    result.write_text(
        "file,threshold,tesseract %,tesseract gate\n"
        "sample.pdf.page-001.raster.png,90,missing_reference,missing_reference\n"
        "missing.pdf.page-001.raster.png,90,not_checked,not_checked\n",
        encoding="utf-8",
    )
    gate = _load_gate_module()

    assert (
        gate.main(
            [
                "--result",
                str(result),
                "--require-complete-scoring",
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "tesseract=missing_reference" in output
    assert "tesseract=not_checked" in output


def test_required_complete_scoring_rejects_empty_corpus(tmp_path, capsys):
    result = tmp_path / "result.csv"
    result.write_text(
        "file,threshold,tesseract %,tesseract gate\n",
        encoding="utf-8",
    )
    gate = _load_gate_module()

    assert (
        gate.main(
            [
                "--result",
                str(result),
                "--require-complete-scoring",
            ]
        )
        == 1
    )
    assert "result contains no corpus rows" in capsys.readouterr().out
