from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
QUALITY_TEXT = {
    "english": "ENGLISH ABCXYZ abcxyz 0123456789",
    "russian": "РУССКИЙ АБВГДЕЖЗ абвгдежз",
    "chinese": "中文测试 汉字识别",
    # Keep the Latin segment distinguishable from Cyrillic when rus+eng are
    # active together. ABC and АВС are glyph-identical in the fixture font.
    "mixed": "MIXED LATIN Д 12345 中文",
}


def _font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)
    raise RuntimeError(
        "Strict OCR fixtures require Noto CJK fonts. Install fonts-noto-cjk "
        "or generate fixtures inside the OCR Docker image."
    )


def generate_quality_image() -> Image.Image:
    img = Image.new("RGB", (1800, 900), "white")
    draw = ImageDraw.Draw(img)
    font = _font(64)
    small = _font(42)

    y = 80
    for label, text in QUALITY_TEXT.items():
        draw.text((80, y), f"{label.upper()}:", fill=(0, 0, 0), font=small)
        draw.text((80, y + 56), text, fill=(0, 0, 0), font=font)
        y += 190

    return img


def write_quality_fixtures() -> Path:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    img = generate_quality_image()
    img.save(FIXTURE_DIR / "multilingual.png", format="PNG")
    img.save(FIXTURE_DIR / "multilingual.jpg", format="JPEG", quality=96)
    img.save(FIXTURE_DIR / "multilingual.webp", format="WEBP", quality=96)
    img.save(FIXTURE_DIR / "multilingual.pdf", format="PDF", resolution=150.0)
    (FIXTURE_DIR / "multilingual.txt").write_text("\n".join(QUALITY_TEXT.values()), encoding="utf-8")
    return FIXTURE_DIR


if __name__ == "__main__":
    print(write_quality_fixtures())
