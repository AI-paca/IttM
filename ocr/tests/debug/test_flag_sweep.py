import importlib.util
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")
load_workbook = openpyxl.load_workbook


REPO_ROOT = Path(__file__).resolve().parents[3]
SWEEP_PATH = REPO_ROOT / "scripts" / "debug" / "debug_flag_sweep.py"


def _load_sweep_module():
    spec = importlib.util.spec_from_file_location("debug_flag_sweep", SWEEP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_writes_highlighted_xlsx(tmp_path):
    sweep = _load_sweep_module()
    rows = [
        {
            "file": "sample.png",
            "match_percent": "95.00",
            "matched_lines": "19",
            "total_lines": "20",
            "seconds": "0.500",
            "scale": "2",
            "preprocess": "autocontrast",
            "psm": "6",
            "lang": "eng+rus",
        },
        {
            "file": "sample.png",
            "match_percent": "80.00",
            "matched_lines": "16",
            "total_lines": "20",
            "seconds": "0.400",
            "scale": "1",
            "preprocess": "rgb",
            "psm": "3",
            "lang": "eng+rus",
        },
    ]

    output = tmp_path / "flag-sweep.xlsx"
    sweep._write_xlsx(output, rows)

    workbook = load_workbook(output)
    sheet = workbook["flag-sweep"]
    assert [cell.value for cell in sheet[1]][:3] == [
        "file",
        "match_percent",
        "matched_lines",
    ]
    assert sheet["A2"].fill.fgColor.rgb == "00FFF2CC"
    assert sheet["A3"].fill.fgColor.rgb != "00FFF2CC"
