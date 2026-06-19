import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = REPO_ROOT / "scripts" / "debug_quality_gate.py"


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("debug_quality_gate", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_debug_quality_gate_fails_only_real_failures(tmp_path, capsys):
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


def test_debug_quality_gate_can_treat_na_as_failure(tmp_path):
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
