import importlib.util
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[3]
PROBE_PATH = REPO_ROOT / "scripts" / "debug" / "debug_pdf_image_probe.py"


def _load_probe_module():
    spec = importlib.util.spec_from_file_location("debug_pdf_image_probe", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_rasterize_pdf_writes_page_png_jpg_expected(tmp_path, monkeypatch):
    probe = _load_probe_module()
    source = tmp_path / "plan.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    expected_root = tmp_path / "expected"
    expected_root.mkdir()
    (expected_root / "plan.pdf.md").write_text(
        "## Page 1\nfirst\n## Page 2\nsecond\n## Page 3\nthird\n",
        encoding="utf-8",
    )

    def fake_render_pdf(path, *, dpi, max_pages):
        assert path == source
        assert dpi == 150
        assert max_pages == 2
        return [
            Image.new("RGB", (10, 8), "white"),
            Image.new("RGB", (10, 8), "white"),
        ]

    monkeypatch.setattr(probe, "_render_pdf", fake_render_pdf)
    outputs = probe.rasterize_pdf(
        source,
        expected_root=expected_root,
        output_dir=tmp_path / "fixtures",
        probe_reference_root=tmp_path / "probe-reference",
        dpi=150,
        max_pages=2,
        gap=1,
        formats=("png", "jpg"),
    )

    assert [path.name for path in outputs] == [
        "plan.pdf.page-001.raster.png",
        "plan.pdf.page-001.raster.jpg",
        "plan.pdf.page-002.raster.png",
        "plan.pdf.page-002.raster.jpg",
    ]
    assert all(path.exists() for path in outputs)
    expected_by_file = {
        "plan.pdf.page-001.raster.png.md": "## Page 1\nfirst\n",
        "plan.pdf.page-001.raster.jpg.md": "## Page 1\nfirst\n",
        "plan.pdf.page-002.raster.png.md": "## Page 2\nsecond\n",
        "plan.pdf.page-002.raster.jpg.md": "## Page 2\nsecond\n",
    }
    for file_name, expected_text in expected_by_file.items():
        expected = tmp_path / "probe-reference" / file_name
        assert expected.read_text(encoding="utf-8") == expected_text


def test_rasterize_pdf_stack_pages_keeps_limited_expected(tmp_path, monkeypatch):
    probe = _load_probe_module()
    source = tmp_path / "plan.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    expected_root = tmp_path / "expected"
    expected_root.mkdir()
    (expected_root / "plan.pdf.md").write_text(
        "## Page 1\nfirst\n## Page 2\nsecond\n## Page 3\nthird\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        probe,
        "_render_pdf",
        lambda path, *, dpi, max_pages: [
            Image.new("RGB", (10, 8), "white"),
            Image.new("RGB", (10, 8), "white"),
        ],
    )

    outputs = probe.rasterize_pdf(
        source,
        expected_root=expected_root,
        output_dir=tmp_path / "fixtures",
        probe_reference_root=tmp_path / "probe-reference",
        dpi=150,
        max_pages=2,
        gap=1,
        stack_pages=True,
        formats=("png",),
    )

    assert [path.name for path in outputs] == ["plan.pdf.raster.png"]
    expected = tmp_path / "probe-reference" / "plan.pdf.raster.png.md"
    assert expected.read_text(encoding="utf-8") == "## Page 1\nfirst\n## Page 2\nsecond\n"


def test_rasterize_pdf_does_not_guess_unsegmented_page_expected(
    tmp_path,
    monkeypatch,
):
    probe = _load_probe_module()
    source = tmp_path / "plan.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    expected_root = tmp_path / "expected"
    expected_root.mkdir()
    (expected_root / "plan.pdf.md").write_text(
        "one combined expected document\nwithout page boundaries\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        probe,
        "_render_pdf",
        lambda path, *, dpi, max_pages: [
            Image.new("RGB", (10, 8), "white"),
            Image.new("RGB", (10, 8), "white"),
        ],
    )

    outputs = probe.rasterize_pdf(
        source,
        expected_root=expected_root,
        output_dir=tmp_path / "fixtures",
        probe_reference_root=tmp_path / "probe-reference",
        dpi=150,
        max_pages=2,
        gap=1,
        formats=("png",),
    )

    assert len(outputs) == 2
    assert list((tmp_path / "probe-reference").iterdir()) == []


def test_page_expected_texts_keeps_unsegmented_single_page():
    probe = _load_probe_module()

    assert probe._page_expected_texts(
        "single page expected",
        max_pages=5,
        rendered_pages=1,
    ) == ["single page expected\n"]


def test_limited_expected_text_uses_form_feed_pages():
    probe = _load_probe_module()

    assert probe._limited_expected_text("page one\fpage two\fpage three", max_pages=2) == "page one\fpage two\n"
