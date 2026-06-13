import asyncio
import os
from pathlib import Path

import pytest
from PIL import Image
from pdf2image import convert_from_path

from app.chunking.vertical import analyze_document_layout
from app.services import convert_service

Image.MAX_IMAGE_PIXELS = None

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "testtables"
FIXTURES = (
    sorted(path for path in FIXTURE_DIR.iterdir() if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".pdf"})
    if FIXTURE_DIR.exists()
    else []
)
PROJECTED_SLIDE_PHOTO = FIXTURE_DIR / "photo_10_2026-05-12_22-26-36.jpg"
TABLE_FIXTURES = [path for path in FIXTURES if path != PROJECTED_SLIDE_PHOTO]

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_OCR_TABLE_FIXTURES") != "1",
    reason="testtables checks are local/heavy; set RUN_OCR_TABLE_FIXTURES=1 to run them.",
)


def _fixture_pages(path: Path) -> list[Image.Image]:
    if path.suffix.lower() == ".pdf":
        return convert_from_path(
            str(path),
            dpi=180,
            first_page=1,
            last_page=3,
            fmt="png",
        )

    with Image.open(path) as image:
        return [image.convert("RGB")]


def _first_page_with_table(path: Path) -> tuple[Image.Image, list]:
    for image in _fixture_pages(path):
        tables = [
            region for region in analyze_document_layout(image) if region.kind == "table" and region.table is not None
        ]
        if tables:
            return image, tables

    raise AssertionError(f"{path.name} did not expose a table in the first scanned pages")


@pytest.mark.parametrize("fixture_path", TABLE_FIXTURES, ids=lambda path: path.name)
def test_table_fixtures_expose_table_regions(fixture_path: Path):
    _, tables = _first_page_with_table(fixture_path)
    largest_table = max(tables, key=lambda region: len(region.table.cells))

    assert largest_table.table.rows >= 2
    assert largest_table.table.cols >= 2
    assert len(largest_table.table.cells) >= 4


@pytest.mark.parametrize("fixture_path", TABLE_FIXTURES, ids=lambda path: path.name)
def test_convert_service_handles_table_fixture_page(monkeypatch, tmp_path, fixture_path: Path):
    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            return []

        def recognize(self, image, mode="text_mode", psm=6):
            return "cell"

        def info(self):
            return {"engine": "fake"}

    image, _ = _first_page_with_table(fixture_path)
    image_path = tmp_path / "fixture-page.png"
    image.save(image_path)
    monkeypatch.setattr(convert_service, "AutoEngine", lambda prefer_tesseract=True: FakeEngine())

    markdown, meta = asyncio.run(convert_service.convert(image_path, engine_type="auto"))

    assert meta["tables_found"] >= 1
    assert meta["table_cells"] >= 4
    assert "|" in markdown


def test_projected_slide_photo_is_dewarped_not_tableified():
    if not PROJECTED_SLIDE_PHOTO.exists():
        pytest.skip("projected slide photo fixture is not available")

    markdown, meta = asyncio.run(convert_service.convert(PROJECTED_SLIDE_PHOTO, engine_type="tesseract"))

    assert meta["tables_found"] == 0
    assert "Основные аналитические показатели" in markdown
    assert "статистики судов общей юрисдикции" in markdown
