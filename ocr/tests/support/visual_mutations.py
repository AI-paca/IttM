import io
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont


def quality_card_image() -> Image.Image:
    image = Image.new("RGB", (1000, 420), "white")
    draw = ImageDraw.Draw(image)
    draw.text((60, 55), "PRODUCT ALPHA", fill="black", font=_font(72))
    draw.text((60, 175), "TOTAL 12345.67", fill="black", font=_font(72))
    draw.text((60, 295), "ORDER ZX-2026-42", fill="black", font=_font(62))
    return image


def _font(size: int) -> ImageFont.FreeTypeFont:
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    )
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default(size=size)


def visual_mutations(source: Image.Image) -> dict[str, Image.Image]:
    return {
        "low-contrast": _low_contrast(source),
        "dark-mode": _dark_mode(source),
        "salt-pepper": _salt_and_pepper(source),
        "watermark": _watermark(source),
        "jpeg-ringing": _jpeg_ringing(source),
        "subpixel": _subpixel_fringes(source),
        "motion-blur": source.filter(ImageFilter.GaussianBlur(radius=0.8)),
        "glare": _glare(source),
        "perspective": _perspective(source),
        "cropped-edge": source.crop((12, 0, source.width - 12, source.height)),
    }


def close_images(images: dict[str, Image.Image]) -> None:
    for image in images.values():
        image.close()


def _low_contrast(source: Image.Image) -> Image.Image:
    return ImageEnhance.Contrast(source.convert("RGB")).enhance(0.32)


def _dark_mode(source: Image.Image) -> Image.Image:
    grayscale = source.convert("L")
    return grayscale.point(lambda value: 36 + int((255 - value) * 0.55)).convert("RGB")


def _salt_and_pepper(source: Image.Image) -> Image.Image:
    image = source.convert("RGB").copy()
    pixels = image.load()
    rng = random.Random(20260613)
    for _ in range(max(1, image.width * image.height // 150)):
        x = rng.randrange(image.width)
        y = rng.randrange(image.height)
        value = 0 if rng.random() < 0.5 else 255
        pixels[x, y] = (value, value, value)
    return image


def _watermark(source: Image.Image) -> Image.Image:
    image = source.convert("RGBA")
    overlay = Image.new("RGBA", source.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)
    draw.text(
        (source.width // 4, source.height // 3),
        "DRAFT",
        fill=(90, 90, 90, 35),
        font=_font(120),
    )
    result = Image.alpha_composite(image, overlay).convert("RGB")
    image.close()
    overlay.close()
    return result


def _jpeg_ringing(source: Image.Image) -> Image.Image:
    output = io.BytesIO()
    source.convert("RGB").save(output, format="JPEG", quality=5)
    with Image.open(io.BytesIO(output.getvalue())) as encoded:
        return encoded.convert("RGB")


def _subpixel_fringes(source: Image.Image) -> Image.Image:
    grayscale = source.convert("L")
    red = Image.new("L", source.size, 255)
    green = Image.new("L", source.size, 255)
    blue = Image.new("L", source.size, 255)
    red.paste(grayscale, (1, 0))
    green.paste(grayscale, (0, 0))
    blue.paste(grayscale, (-1, 0))
    result = Image.merge("RGB", (red, green, blue))
    grayscale.close()
    red.close()
    green.close()
    blue.close()
    return result


def _glare(source: Image.Image) -> Image.Image:
    image = source.convert("RGBA")
    overlay = Image.new("RGBA", source.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)
    draw.ellipse(
        (
            source.width * 0.58,
            -source.height * 0.2,
            source.width * 1.05,
            source.height * 0.75,
        ),
        fill=(255, 255, 255, 110),
    )
    result = Image.alpha_composite(image, overlay).convert("RGB")
    image.close()
    overlay.close()
    return result


def _perspective(source: Image.Image) -> Image.Image:
    return source.transform(
        source.size,
        Image.Transform.QUAD,
        (
            40,
            20,
            10,
            source.height,
            source.width - 45,
            source.height - 15,
            source.width - 10,
            0,
        ),
        resample=Image.Resampling.BICUBIC,
        fillcolor="white",
    )
