import asyncio
import os
from pathlib import Path

import pytest
from PIL import Image
from pdf2image import convert_from_path

from app.chunking.vertical import analyze_document_layout
from app.services import convert_service

Image.MAX_IMAGE_PIXELS = None

REPO_ROOT = Path(__file__).resolve().parents[3]
SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
configured_fixture_dir = os.environ.get("OCR_DEBUG_DIR")
if configured_fixture_dir:
    FIXTURE_DIR = Path(configured_fixture_dir).expanduser().resolve()
else:
    fixture_candidates = (
        REPO_ROOT / "debug" / "expected" / "input-files",
        REPO_ROOT / "debug",
        REPO_ROOT / "testtables",
    )
    FIXTURE_DIR = next(
        (
            candidate
            for candidate in fixture_candidates
            if candidate.exists() and any(path.suffix.lower() in SUPPORTED_SUFFIXES for path in candidate.iterdir())
        ),
        fixture_candidates[0],
    )
FIXTURES = (
    sorted(path for path in FIXTURE_DIR.iterdir() if path.suffix.lower() in SUPPORTED_SUFFIXES)
    if FIXTURE_DIR.exists()
    else []
)
FIXTURE_ROLES = {
    "000041301_UchebPlan_sign000029629.pdf": "grid-table",
    "09.03.03_05(ИУ1).pdf": "grid-table",
    "Adobe Scan Oct 26, 2022 (1).pdf": "grid-table",
    "IMG_20260613_173055_477.jpg": "product-cards",
    "Screenshot_2026-06-06-13-43-07-99.jpg": "product-cards",
    "Ucheb_plan_020302-2022-O-PP-4y00m-02.pdf": "grid-table",
    "image (10).png": "aligned-pairs",
    "image (5).png": "single-row-scores",
    "image (6).png": "grid-table",
    "image (7).png": "coupon",
    "image (8).png": "coupon",
    "photo_10_2026-05-12_22-26-36.jpg": "projected-text",
    "photo_6_2026-05-12_22-26-36.jpg": "diagram",
    "УП2022 09.03.03 МиКМПиС ФГОС3++.pdf": "grid-table",
}
PROJECTED_SLIDE_PHOTO = FIXTURE_DIR / "photo_10_2026-05-12_22-26-36.jpg"
TABLE_FIXTURES = [path for path in FIXTURES if FIXTURE_ROLES.get(path.name) == "grid-table"]

pytestmark = pytest.mark.skipif(
    os.environ.get(
        "RUN_OCR_DEBUG",
        os.environ.get("RUN_OCR_TABLE_FIXTURES"),
    )
    != "1",
    reason="debug checks are local/heavy; set RUN_OCR_DEBUG=1 to run them.",
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


def test_fixture_manifest_covers_the_local_debug_set():
    assert {path.name for path in FIXTURES} == set(FIXTURE_ROLES)


@pytest.mark.parametrize("fixture_path", FIXTURES, ids=lambda path: path.name)
def test_debug_fixtures_decode(fixture_path: Path):
    pages = _fixture_pages(fixture_path)
    try:
        assert pages
        assert all(page.width > 0 and page.height > 0 for page in pages)
    finally:
        for page in pages:
            page.close()


@pytest.mark.parametrize("fixture_path", TABLE_FIXTURES, ids=lambda path: path.name)
def test_grid_table_fixtures_expose_table_regions(fixture_path: Path):
    _, tables = _first_page_with_table(fixture_path)
    largest_table = max(tables, key=lambda region: len(region.table.cells))

    assert largest_table.table.rows >= 2
    assert largest_table.table.cols >= 2
    assert len(largest_table.table.cells) >= 4


def test_projected_slide_photo_is_dewarped_not_tableified():
    if not PROJECTED_SLIDE_PHOTO.exists():
        pytest.skip("projected slide photo fixture is not available")

    markdown, meta = asyncio.run(convert_service.convert(PROJECTED_SLIDE_PHOTO, engine_type="tesseract"))

    assert meta["tables_found"] == 0
    assert "Основные аналитические показатели" in markdown
    assert "статистики судов общей юрисдикции" in markdown
