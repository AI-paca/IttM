import io
import random
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

GENERATED_FIXTURE_GENERATOR_VERSION = "2026.06.14-1"
FUNCTIONAL_OCR_GENERATOR_VERSION = "2026.06.14-functional-1"


@dataclass(frozen=True)
class GeneratedFixtureSpec:
    id: str
    seed: int
    category: str
    tier: str
    expected_tokens: tuple[str, ...]
    generator_version: str = GENERATED_FIXTURE_GENERATOR_VERSION
    expected_pairs: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class FunctionalOcrFixtureSpec:
    id: str
    seed: int
    category: str
    tier: str
    expected_tokens: tuple[str, ...]
    generator_version: str = FUNCTIONAL_OCR_GENERATOR_VERSION
    expected_pairs: tuple[tuple[str, str], ...] = ()
    table_shape: tuple[int, int] | None = None


GENERATED_FIXTURE_REGISTRY = (
    GeneratedFixtureSpec(
        id="long-screenshot-receipt",
        seed=2026061401,
        category="long_screenshot",
        tier="contract",
        expected_tokens=("ITEM-001", "TOTAL"),
    ),
    GeneratedFixtureSpec(
        id="structured-product-table",
        seed=2026061402,
        category="table",
        tier="contract",
        expected_tokens=("PRODUCT-001", "12345.67"),
        expected_pairs=(("PRODUCT-001", "12345.67"),),
    ),
    GeneratedFixtureSpec(
        id="full-width-banner",
        seed=2026061403,
        category="banner",
        tier="contract",
        expected_tokens=("NOTICE", "CONTINUE"),
    ),
    GeneratedFixtureSpec(
        id="mixed-language-card",
        seed=2026061404,
        category="mixed_language",
        tier="contract",
        expected_tokens=("HELLO", "ПРИВЕТ", "你好"),
    ),
)


FUNCTIONAL_OCR_FIXTURE_REGISTRY = (
    FunctionalOcrFixtureSpec(
        id="generated-simple-paragraph",
        seed=2026061451,
        category="simple_paragraph",
        tier="quality",
        expected_tokens=("ALPHA PROJECT", "ORDER", "ZX-2026-42", "12345", "ALICE"),
        expected_pairs=(("ORDER", "ZX-2026-42"),),
    ),
    FunctionalOcrFixtureSpec(
        id="generated-product-table",
        seed=2026061452,
        category="table",
        tier="quality",
        expected_tokens=("ITEM", "ALPHA", "BETA", "123.45", "987.65", "A-100", "B-200"),
        expected_pairs=(("ALPHA", "123.45"), ("BETA", "987.65")),
        table_shape=(3, 4),
    ),
    FunctionalOcrFixtureSpec(
        id="generated-low-contrast-noise",
        seed=2026061453,
        category="degraded_text",
        tier="quality",
        expected_tokens=("GAMMA CHECK", "NOISE-2048", "45678", "TOKEN"),
        expected_pairs=(("TOKEN", "45678"),),
    ),
    FunctionalOcrFixtureSpec(
        id="generated-small-skew",
        seed=2026061454,
        category="skewed_text",
        tier="quality",
        expected_tokens=("DELTA RECEIPT", "SKEW-17", "TOTAL", "24680"),
        expected_pairs=(("TOTAL", "24680"),),
    ),
    FunctionalOcrFixtureSpec(
        id="generated-tiny-score-list",
        seed=2026061455,
        category="tiny_score_list",
        tier="quality",
        expected_tokens=(
            "ALPHA",
            "BRAVO",
            "CHARLIE",
            "DELTA",
            "ECHO",
            "10",
            "8",
            "7",
            "6",
        ),
        expected_pairs=(
            ("ALPHA", "10"),
            ("BRAVO", "8"),
            ("CHARLIE", "7"),
            ("DELTA", "6"),
        ),
    ),
)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/TTF/DejaVuSans.ttf"),
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
        (
            "/usr/share/fonts/liberation/LiberationSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/liberation/LiberationSans-Regular.ttf"
        ),
        (
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"
        ),
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    )
    for path in candidates:
        if Path(path).exists() or not path.startswith("/"):
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _draw_lines(image, lines, *, x=80, y=70, spacing=92, size=54, fill=None, bold=False):
    if fill is None:
        fill = "black"
    draw = ImageDraw.Draw(image)
    font = _font(size, bold=bold)
    for index, line in enumerate(lines):
        draw.text((x, y + index * spacing), line, fill=fill, font=font)


def _simple_paragraph_fixture() -> Image.Image:
    image = Image.new("RGB", (1600, 720), "white")
    _draw_lines(
        image,
        [
            "ALPHA PROJECT STATUS",
            "ORDER ZX-2026-42",
            "Reliable OCR keeps digits 12345",
            "Owner ALICE reviews the report",
        ],
        size=58,
        spacing=116,
        bold=True,
    )
    return image


def _product_table_fixture() -> Image.Image:
    image = Image.new("RGB", (1300, 640), "white")
    draw = ImageDraw.Draw(image)
    font = _font(46)
    header_font = _font(48, bold=True)
    x_lines = (60, 430, 650, 930, 1230)
    y_lines = (70, 220, 370, 520)

    for x in x_lines:
        draw.line((x, y_lines[0], x, y_lines[-1]), fill="black", width=4)
    for y in y_lines:
        draw.line((x_lines[0], y, x_lines[-1], y), fill="black", width=4)

    rows = (
        ("ITEM", "QTY", "PRICE", "CODE"),
        ("ALPHA", "2", "123.45", "A-100"),
        ("BETA", "5", "987.65", "B-200"),
    )
    for row_index, row in enumerate(rows):
        y = y_lines[row_index] + 48
        for col_index, value in enumerate(row):
            x = x_lines[col_index] + 34
            draw.text(
                (x, y),
                value,
                fill="black",
                font=header_font if row_index == 0 else font,
            )
    return image


def _low_contrast_noise_fixture(seed: int) -> Image.Image:
    image = Image.new("RGB", (1500, 620), (246, 246, 238))
    _draw_lines(
        image,
        [
            "GAMMA CHECK",
            "TOKEN 45678",
            "NOISE-2048 remains readable",
        ],
        size=62,
        spacing=128,
        fill=(72, 72, 72),
        bold=True,
    )
    image = ImageEnhance.Contrast(image).enhance(0.62)
    pixels = image.load()
    if pixels is None:
        return image
    rng = random.Random(seed)
    for _ in range((image.width * image.height) // 280):
        x = rng.randrange(image.width)
        y = rng.randrange(image.height)
        value = 35 if rng.random() < 0.5 else 225
        pixels[x, y] = (value, value, value)
    return image


def _small_skew_fixture() -> Image.Image:
    image = Image.new("RGB", (1450, 620), "white")
    _draw_lines(
        image,
        [
            "DELTA RECEIPT",
            "SKEW-17 SAMPLE",
            "TOTAL 24680",
        ],
        size=68,
        spacing=136,
        bold=True,
    )
    rotated = image.rotate(1.4, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
    image.close()
    return rotated


def _tiny_score_list_fixture() -> Image.Image:
    image = Image.new("RGB", (180, 330), "white")
    draw = ImageDraw.Draw(image)
    font = _font(16, bold=True)
    small = _font(14)
    draw.text((12, 12), "NAME", fill="black", font=small)
    draw.text((124, 12), "SCORE", fill="black", font=small)
    rows = (
        ("ALPHA", "10"),
        ("BRAVO", "8"),
        ("CHARLIE", "7"),
        ("DELTA", "6"),
        ("ECHO", "9"),
    )
    for index, (name, score) in enumerate(rows):
        y = 52 + index * 48
        draw.text((12, y), name, fill="black", font=font)
        draw.text((140, y), score, fill="black", font=font)
    return image


def functional_ocr_fixture_spec(fixture_id: str) -> FunctionalOcrFixtureSpec:
    for spec in FUNCTIONAL_OCR_FIXTURE_REGISTRY:
        if spec.id == fixture_id:
            return spec
    known = ", ".join(spec.id for spec in FUNCTIONAL_OCR_FIXTURE_REGISTRY)
    raise KeyError(f"Unknown functional OCR fixture '{fixture_id}'. Known fixtures: {known}")


def functional_ocr_fixture_image(fixture_id: str) -> Image.Image:
    spec = functional_ocr_fixture_spec(fixture_id)
    if spec.category == "simple_paragraph":
        return _simple_paragraph_fixture()
    if spec.category == "table":
        return _product_table_fixture()
    if spec.category == "degraded_text":
        return _low_contrast_noise_fixture(spec.seed)
    if spec.category == "skewed_text":
        return _small_skew_fixture()
    if spec.category == "tiny_score_list":
        return _tiny_score_list_fixture()
    raise ValueError(f"Unsupported functional OCR fixture category '{spec.category}'")


def functional_ocr_fixture_bytes(fixture_id: str, *, image_format="PNG", **save_options):
    image = functional_ocr_fixture_image(fixture_id)
    try:
        output = io.BytesIO()
        image.save(output, format=image_format, **save_options)
        return output.getvalue()
    finally:
        image.close()


def image_bytes(
    *,
    mode="RGB",
    size=(96, 64),
    image_format="PNG",
    color=None,
    **save_options,
):
    if color is None:
        color = {
            "1": 1,
            "I;16": 32768,
            "L": 220,
            "CMYK": (0, 30, 50, 10),
            "RGBA": (255, 255, 255, 0),
            "RGB": (245, 245, 245),
        }[mode]
    image = Image.new(mode, size, color)
    output = io.BytesIO()
    image.save(output, format=image_format, **save_options)
    image.close()
    return output.getvalue()


def text_image_bytes(*, size=(480, 180), image_format="PNG", **save_options):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.text((24, 35), "PRODUCT-001", fill="black")
    draw.text((24, 95), "12345.67", fill="black")
    output = io.BytesIO()
    image.save(output, format=image_format, **save_options)
    image.close()
    return output.getvalue()


def animated_gif_bytes(frame_count=5):
    frames = []
    for index in range(frame_count):
        frame = Image.new("RGB", (120, 80), "white")
        ImageDraw.Draw(frame).text((10, 30), f"FRAME-{index}", fill="black")
        frames.append(frame)

    output = io.BytesIO()
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=1,
        loop=0,
    )
    for frame in frames:
        frame.close()
    return output.getvalue()


def multipage_tiff_bytes(page_count=3):
    pages = [Image.new("RGB", (120, 80), (240 - index * 10,) * 3) for index in range(page_count)]
    output = io.BytesIO()
    pages[0].save(
        output,
        format="TIFF",
        save_all=True,
        append_images=pages[1:],
    )
    for page in pages:
        page.close()
    return output.getvalue()


def exif_rotated_jpeg_bytes():
    image = Image.new("RGB", (160, 80), "white")
    ImageDraw.Draw(image).text((12, 25), "ROTATE", fill="black")
    exif = Image.Exif()
    exif[274] = 6
    output = io.BytesIO()
    image.save(output, format="JPEG", exif=exif)
    image.close()
    return output.getvalue()


def transparent_text_png_bytes():
    image = Image.new("RGBA", (180, 80), (255, 255, 255, 0))
    ImageDraw.Draw(image).text((15, 25), "ALPHA", fill=(0, 0, 0, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    image.close()
    return output.getvalue()


def deterministic_mutations(seed: bytes, count=32):
    rng = random.Random(20260613)
    for _ in range(count):
        mutation = bytearray(seed)
        for _ in range(1 + rng.randrange(8)):
            if not mutation:
                break
            offset = rng.randrange(len(mutation))
            mutation[offset] ^= 1 << rng.randrange(8)
        yield bytes(mutation)
