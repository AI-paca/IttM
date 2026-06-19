import importlib.util
import json
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[3]
PREPARE_PATH = REPO_ROOT / "scripts" / "debug" / "prepare-browser-ocr-fixture.py"


def _load_prepare_module():
    spec = importlib.util.spec_from_file_location("prepare_browser_ocr_fixture", PREPARE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_prepare_browser_fixture_applies_profile_resize(tmp_path):
    prepare = _load_prepare_module()
    source = tmp_path / "source.png"
    output = tmp_path / "prepared.png"
    profile_json = tmp_path / "profile.json"

    image = Image.new("RGB", (400, 200), "white")
    try:
        image.save(source)
    finally:
        image.close()
    profile_json.write_text(
        json.dumps(
            {
                "imagePreprocessing": ["browser_resize"],
                "maxDimension": 100,
                "maxImagePixels": 10_000,
            }
        ),
        encoding="utf-8",
    )

    assert (
        prepare.main(
            [
                str(source),
                str(output),
                "--profile-json",
                str(profile_json),
            ]
        )
        == 0
    )

    with Image.open(output) as prepared:
        assert prepared.size == (100, 50)
