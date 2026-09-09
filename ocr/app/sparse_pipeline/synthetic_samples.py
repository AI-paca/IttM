from __future__ import annotations

import hashlib
import itertools
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

LANGUAGE_LINES: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "paragraph": (
            "A simple document keeps enough context for recognition.",
            "Every known line is generated before OCR is called.",
            "Spacing, margins, fonts and borders vary independently.",
        ),
        "list": ("1. First item", "2. Second item", "3. Third item"),
        "table": ("Name | Value", "Alpha | 17", "Beta | 204"),
    },
    "ru": {
        "paragraph": (
            "Простой документ сохраняет контекст для распознавания.",
            "Каждая известная строка создаётся до запуска OCR.",
            "Интервалы, поля, шрифты и рамки меняются независимо.",
        ),
        "list": ("1. Первый пункт", "2. Второй пункт", "3. Третий пункт"),
        "table": ("Имя | Значение", "Альфа | 17", "Бета | 204"),
    },
    "zh": {
        "paragraph": (
            "简单文档保留足够的识别上下文。",
            "每一行已知文本都在识别之前生成。",
            "行距页边距字体和边框分别变化。",
        ),
        "list": ("1. 第一项", "2. 第二项", "3. 第三项"),
        "table": ("名称 | 数值", "甲 | 17", "乙 | 204"),
    },
}

FONT_CATALOG: dict[str, tuple[str, ...]] = {
    "en": (
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSerif.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/liberation/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSerifCJK-Regular.ttc",
    ),
    "ru": (
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSerif.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/liberation/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSerifCJK-Regular.ttc",
    ),
    "zh": (
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSerifCJK-Regular.ttc",
    ),
    "mixed": (
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSerifCJK-Regular.ttc",
    ),
}

COLOR_SCHEMES = (
    ((255, 255, 255), (0, 0, 0)),
    ((246, 243, 235), (12, 13, 15)),
    ((28, 31, 38), (242, 244, 247)),
)

LINE_OWNER_SCHEMA_VERSION = 1
LINE_OWNER_ENCODING = "uint16-v1:0=background,1..65534=line,65535=frame"
LINE_OWNER_FRAME = int(np.iinfo(np.uint16).max)
LINE_OWNER_SUFFIX = ".line-owner.mask.png"


@dataclass(frozen=True)
class SyntheticSpec:
    case_id: str
    language: str
    layout: str
    line_spacing: int
    margin_pt: int
    border_pt: int
    font_size: int
    font_path: str
    dpi: int = 300
    background_rgb: tuple[int, int, int] = (255, 255, 255)
    foreground_rgb: tuple[int, int, int] = (0, 0, 0)

    def __post_init__(self) -> None:
        if self.language not in {*LANGUAGE_LINES, "mixed"}:
            raise ValueError(f"unsupported language: {self.language}")
        if self.layout not in {"paragraph", "list", "table", "combined"}:
            raise ValueError(f"unsupported layout: {self.layout}")
        if not 0 <= self.line_spacing <= 10:
            raise ValueError("line_spacing must be between zero and ten")
        if not 0 <= self.margin_pt <= 3 or not 0 <= self.border_pt <= 3:
            raise ValueError("margin and border must be between zero and three points")
        if self.font_size < 6 or self.dpi < 72:
            raise ValueError("font size or DPI is too small")


@dataclass(frozen=True)
class SyntheticSample:
    spec: SyntheticSpec
    image: Image.Image
    expected_text: str
    expected_ink_mask: np.ndarray
    expected_text_mask: np.ndarray
    expected_frame_mask: np.ndarray
    expected_line_masks: tuple[np.ndarray, ...]


def available_fonts(language: str) -> tuple[str, ...]:
    values = tuple(path for path in FONT_CATALOG[language] if Path(path).is_file())
    if not values:
        raise FileNotFoundError(f"no local font can render {language}")
    return values


def known_lines(language: str, layout: str) -> tuple[str, ...]:
    if language == "mixed":
        return tuple(
            itertools.chain.from_iterable(
                (f"[{item_language.upper()}]",)
                + LANGUAGE_LINES[item_language]["paragraph"]
                + LANGUAGE_LINES[item_language]["list"]
                + LANGUAGE_LINES[item_language]["table"]
                for item_language in ("en", "ru", "zh")
            )
        )
    if layout == "combined":
        return tuple(
            itertools.chain.from_iterable(
                LANGUAGE_LINES[language][item_layout] for item_layout in ("paragraph", "list", "table")
            )
        )
    return LANGUAGE_LINES[language][layout]


def render_sample(spec: SyntheticSpec) -> SyntheticSample:
    lines = known_lines(spec.language, spec.layout)
    font = ImageFont.truetype(spec.font_path, spec.font_size)
    probe = Image.new("RGB", (1, 1), spec.background_rgb)
    probe_draw = ImageDraw.Draw(probe)
    measurements = tuple(probe_draw.textbbox((0, 0), line, font=font, anchor="lt") for line in lines)
    line_height = max(1, max(box[3] - box[1] for box in measurements))
    minimum_text_left = min(box[0] for box in measurements)
    maximum_text_right = max(box[2] for box in measurements)
    text_width = max(1, maximum_text_right - minimum_text_left)
    margin_pixels = round(spec.margin_pt * spec.dpi / 72)
    border_pixels = round(spec.border_pt * spec.dpi / 72)
    padding = margin_pixels + border_pixels
    width = max(1, text_width + 2 * padding)
    height = max(1, len(lines) * line_height + (len(lines) - 1) * spec.line_spacing + 2 * padding)
    image = Image.new("RGB", (width, height), spec.background_rgb)
    draw = ImageDraw.Draw(image)
    text_mask_image = Image.new("L", (width, height), 0)
    text_mask_draw = ImageDraw.Draw(text_mask_image)
    frame_mask_image = Image.new("L", (width, height), 0)
    frame_mask_draw = ImageDraw.Draw(frame_mask_image)
    if border_pixels:
        frame_box = (0, 0, width - 1, height - 1)
        draw.rectangle(frame_box, outline=spec.foreground_rgb, width=border_pixels)
        frame_mask_draw.rectangle(frame_box, outline=255, width=border_pixels)
    y = padding
    x = padding - minimum_text_left
    line_masks: list[np.ndarray] = []
    for line, measurement in zip(lines, measurements):
        origin = (x, y - measurement[1])
        draw.text(origin, line, font=font, fill=spec.foreground_rgb, anchor="lt")
        text_mask_draw.text(origin, line, font=font, fill=255, anchor="lt")
        line_mask_image = Image.new("L", (width, height), 0)
        ImageDraw.Draw(line_mask_image).text(origin, line, font=font, fill=255, anchor="lt")
        line_masks.append(np.asarray(line_mask_image, dtype=np.uint8) > 0)
        y += line_height + spec.line_spacing
    text_mask = np.asarray(text_mask_image, dtype=np.uint8) > 0
    frame_mask = np.asarray(frame_mask_image, dtype=np.uint8) > 0
    if np.logical_and(text_mask, frame_mask).any():
        raise RuntimeError("synthetic text overlaps the page frame")
    line_coverage = np.zeros((height, width), dtype=np.uint8)
    for line_mask in line_masks:
        line_coverage += line_mask
    if np.any(line_coverage > 1):
        raise RuntimeError("synthetic line masks overlap")
    if not np.array_equal(line_coverage > 0, text_mask):
        raise RuntimeError("synthetic line masks do not reconstruct the text mask")
    mask = np.logical_or(text_mask, frame_mask)
    for value in (mask, text_mask, frame_mask, *line_masks):
        value.setflags(write=False)
    return SyntheticSample(
        spec=spec,
        image=image,
        expected_text="\n".join(lines),
        expected_ink_mask=mask,
        expected_text_mask=text_mask,
        expected_frame_mask=frame_mask,
        expected_line_masks=tuple(line_masks),
    )


def _case_id(parts: tuple[object, ...]) -> str:
    raw = "-".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def smoke_specs() -> tuple[SyntheticSpec, ...]:
    values: list[SyntheticSpec] = []
    for language, spacing, margin, font_size, layout in itertools.product(
        ("en", "ru", "zh"),
        (0, 5, 10),
        (0, 3),
        (14, 30),
        ("paragraph", "list"),
    ):
        fonts = available_fonts(language)
        font_path = fonts[(spacing + margin + font_size + len(layout)) % len(fonts)]
        background, foreground = COLOR_SCHEMES[(spacing + margin + font_size) % len(COLOR_SCHEMES)]
        parts = (
            language,
            layout,
            spacing,
            margin,
            margin,
            font_size,
            Path(font_path).name,
        )
        values.append(
            SyntheticSpec(
                case_id=_case_id(parts),
                language=language,
                layout=layout,
                line_spacing=spacing,
                margin_pt=margin,
                border_pt=margin,
                font_size=font_size,
                font_path=font_path,
                background_rgb=background,
                foreground_rgb=foreground,
            )
        )
    mixed_font = available_fonts("mixed")[0]
    values.append(
        SyntheticSpec(
            case_id="mixed-ultra",
            language="mixed",
            layout="combined",
            line_spacing=0,
            margin_pt=0,
            border_pt=3,
            font_size=18,
            font_path=mixed_font,
            background_rgb=(246, 243, 235),
            foreground_rgb=(12, 13, 15),
        )
    )
    return tuple(values)


def tiny_specs() -> tuple[SyntheticSpec, ...]:
    """Exhaustive small-font grid used to prove touching-row separation."""

    values: list[SyntheticSpec] = []
    index = 0
    for language in ("en", "ru", "zh"):
        for font_path in available_fonts(language):
            for font_size in range(6, 10):
                for spacing in range(11):
                    for layout in ("paragraph", "list", "table", "combined"):
                        background, foreground = COLOR_SCHEMES[index % len(COLOR_SCHEMES)]
                        values.append(
                            SyntheticSpec(
                                case_id=f"tiny-{index:04d}",
                                language=language,
                                layout=layout,
                                line_spacing=spacing,
                                margin_pt=index % 4,
                                border_pt=(index // 4) % 4,
                                font_size=font_size,
                                font_path=font_path,
                                background_rgb=background,
                                foreground_rgb=foreground,
                            )
                        )
                        index += 1
    return tuple(values)


def full_specs() -> tuple[SyntheticSpec, ...]:
    """A 1,232-case grid; fonts/layouts/colors rotate without random sampling."""

    values: list[SyntheticSpec] = []
    layouts = ("paragraph", "list", "table", "combined")
    for language, spacing, margin, border, font_size in itertools.product(
        ("en", "ru", "zh"),
        range(11),
        range(4),
        range(4),
        (10, 24),
    ):
        fonts = available_fonts(language)
        selector = spacing + 3 * margin + 5 * border + font_size
        font_path = fonts[selector % len(fonts)]
        layout = layouts[selector % len(layouts)]
        background, foreground = COLOR_SCHEMES[selector % len(COLOR_SCHEMES)]
        parts = (
            language,
            layout,
            spacing,
            margin,
            border,
            font_size,
            Path(font_path).name,
        )
        values.append(
            SyntheticSpec(
                case_id=_case_id(parts),
                language=language,
                layout=layout,
                line_spacing=spacing,
                margin_pt=margin,
                border_pt=border,
                font_size=font_size,
                font_path=font_path,
                background_rgb=background,
                foreground_rgb=foreground,
            )
        )
    mixed_font = available_fonts("mixed")[0]
    for spacing, margin, border in itertools.product(range(11), range(4), range(4)):
        parts = (
            "mixed",
            "combined",
            spacing,
            margin,
            border,
            18,
            Path(mixed_font).name,
        )
        values.append(
            SyntheticSpec(
                case_id=_case_id(parts),
                language="mixed",
                layout="combined",
                line_spacing=spacing,
                margin_pt=margin,
                border_pt=border,
                font_size=18,
                font_path=mixed_font,
            )
        )
    return tuple(values)


def write_samples(root: Path, specs: tuple[SyntheticSpec, ...]) -> Path:
    destination = root.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.partial-",
            dir=destination.parent,
        )
    )
    try:
        manifest_lines = []
        for spec in specs:
            sample = render_sample(spec)
            stem = f"{spec.case_id}-{spec.language}-{spec.layout}"
            image_path = staging / f"{stem}.png"
            text_path = staging / f"{stem}.txt"
            mask_path = staging / f"{stem}.mask.png"
            line_owner_path = staging / f"{stem}{LINE_OWNER_SUFFIX}"
            sample.image.save(image_path, format="PNG", dpi=(spec.dpi, spec.dpi))
            text_path.write_text(sample.expected_text + "\n", encoding="utf-8")
            Image.fromarray(sample.expected_ink_mask.astype(np.uint8) * 255, mode="L").save(
                mask_path,
                format="PNG",
            )
            if len(sample.expected_line_masks) >= LINE_OWNER_FRAME:
                raise ValueError("line owner oracle exceeds the uint16 label limit")
            line_owner = np.zeros(sample.expected_ink_mask.shape, dtype=np.uint16)
            line_owner[sample.expected_frame_mask] = LINE_OWNER_FRAME
            for line_index, line_mask in enumerate(sample.expected_line_masks, start=1):
                line_owner[line_mask] = line_index
            Image.fromarray(line_owner).save(line_owner_path, format="PNG")
            manifest_lines.append(
                json.dumps(
                    {
                        **asdict(spec),
                        "schema_version": 2,
                        "image": image_path.name,
                        "reference": text_path.name,
                        "mask": mask_path.name,
                        "line_owner_mask": line_owner_path.name,
                        "line_owner_schema_version": LINE_OWNER_SCHEMA_VERSION,
                        "line_owner_encoding": LINE_OWNER_ENCODING,
                        "known_lines": len(sample.expected_line_masks),
                        "width": sample.image.width,
                        "height": sample.image.height,
                        "ink_pixels": int(sample.expected_ink_mask.sum()),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        (staging / "manifest.jsonl").write_text(
            "\n".join(manifest_lines) + "\n",
            encoding="utf-8",
        )
        staging.replace(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination
