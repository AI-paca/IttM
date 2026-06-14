import io
import random
from dataclasses import dataclass

from PIL import Image, ImageDraw


GENERATED_FIXTURE_GENERATOR_VERSION = "2026.06.14-1"


@dataclass(frozen=True)
class GeneratedFixtureSpec:
    id: str
    seed: int
    category: str
    tier: str
    expected_tokens: tuple[str, ...]
    generator_version: str = GENERATED_FIXTURE_GENERATOR_VERSION
    expected_pairs: tuple[tuple[str, str], ...] = ()


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
