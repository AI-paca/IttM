from __future__ import annotations

import hashlib
import importlib
import importlib.util
import io
import re
from dataclasses import dataclass
from enum import Enum

import numpy as np
from PIL import Image, UnidentifiedImageError

GAMMA_DARK = 1.2
RECIPE_ID = "kornia-gamma-dark-v1"
DARK_SMALL_TEXT_NORMALIZATION_ID = (
    "dark-sparse-xheight-kornia-gamma-source-scale-v1"
)
CANDIDATE_ROLE = "optional-preprocessing-candidate"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SAFE_CROP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DARK_BACKGROUND_MAX = 96.0
_DARK_TEXT_CONTRAST_MIN = 96.0
_DARK_TEXT_FOREGROUND_MIN = 0.003
_DARK_TEXT_FOREGROUND_MAX = 0.05
_DARK_TEXT_COMPONENTS_MIN = 8
_DARK_TEXT_X_HEIGHT_MIN = 5.0
_DARK_TEXT_X_HEIGHT_MAX = 14.0


def _otsu_foreground(grayscale: np.ndarray) -> tuple[float, np.ndarray]:
    histogram = np.bincount(grayscale.ravel(), minlength=256).astype(
        np.float64
    )
    total = float(grayscale.size)
    weighted_total = float(
        np.dot(np.arange(256, dtype=np.float64), histogram)
    )
    background_weight = 0.0
    background_sum = 0.0
    best_variance = -1.0
    best_threshold = 0
    for threshold, count in enumerate(histogram):
        background_weight += float(count)
        if background_weight <= 0.0:
            continue
        foreground_weight = total - background_weight
        if foreground_weight <= 0.0:
            break
        background_sum += threshold * float(count)
        background_mean = background_sum / background_weight
        foreground_mean = (
            weighted_total - background_sum
        ) / foreground_weight
        variance = (
            background_weight
            * foreground_weight
            * (background_mean - foreground_mean) ** 2
        )
        if variance > best_variance:
            best_variance = variance
            best_threshold = threshold
    return float(best_threshold), grayscale > best_threshold


def _foreground_component_stats(
    foreground: np.ndarray,
) -> tuple[tuple[int, int, int], ...]:
    """Return width, height, and area for 8-connected row-run components."""

    parent: list[int] = []
    runs: list[tuple[int, int, int, int]] = []
    previous: list[tuple[int, int, int]] = []

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    for top, row in enumerate(foreground):
        padded = np.pad(row, (1, 1), constant_values=False)
        transitions = np.diff(padded.astype(np.int8))
        starts = np.flatnonzero(transitions == 1)
        stops = np.flatnonzero(transitions == -1)
        current: list[tuple[int, int, int]] = []
        previous_index = 0
        for left_value, right_value in zip(starts, stops):
            left = int(left_value)
            right = int(right_value)
            run_id = len(runs)
            parent.append(run_id)
            runs.append((left, right, top, right - left))
            while (
                previous_index < len(previous)
                and previous[previous_index][1] < left - 1
            ):
                previous_index += 1
            overlap_index = previous_index
            while (
                overlap_index < len(previous)
                and previous[overlap_index][0] <= right
            ):
                union(run_id, previous[overlap_index][2])
                overlap_index += 1
            current.append((left, right, run_id))
        previous = current

    aggregates: dict[int, list[int]] = {}
    for run_id, (left, right, top, area) in enumerate(runs):
        root = find(run_id)
        value = aggregates.setdefault(
            root,
            [left, right, top, top + 1, 0],
        )
        value[0] = min(value[0], left)
        value[1] = max(value[1], right)
        value[2] = min(value[2], top)
        value[3] = max(value[3], top + 1)
        value[4] += area
    return tuple(
        (right - left, bottom - top, area)
        for left, right, top, bottom, area in aggregates.values()
    )


class EnhancementBackend(str, Enum):
    AUTO = "auto"
    NUMPY = "numpy"
    TORCH_CUDA = "torch_cuda"


class EnhancementStatus(str, Enum):
    COMPLETE = "complete"


class CropEnhancementLimitError(RuntimeError):
    """Raised before decoding or batching would exceed a configured bound."""


class CropEnhancementInvariantError(ValueError):
    """Raised when a crop violates the immutable PNG stage boundary."""


class CropEnhancementBackendError(RuntimeError):
    """Raised when the requested execution backend is unavailable or fails."""


@dataclass(frozen=True)
class CropEnhancementConfig:
    backend: EnhancementBackend = EnhancementBackend.AUTO
    max_input_bytes: int = 32 * 1024 * 1024
    max_input_pixels: int = 16_000_000
    max_dimension: int = 16_384
    max_batch_items: int = 256
    max_batch_pixels: int = 64_000_000
    max_output_bytes: int = 64 * 1024 * 1024
    dpi: int = 300

    def __post_init__(self) -> None:
        if not isinstance(self.backend, EnhancementBackend):
            raise ValueError("backend must be an EnhancementBackend")
        limits = (
            self.max_input_bytes,
            self.max_input_pixels,
            self.max_dimension,
            self.max_batch_items,
            self.max_batch_pixels,
            self.max_output_bytes,
            self.dpi,
        )
        if any(type(value) is not int or value < 1 for value in limits):
            raise ValueError("crop enhancement limits must be positive integers")
        if not 1 <= self.dpi <= 2_400:
            raise ValueError("dpi must be between 1 and 2400")
        if self.max_batch_pixels < self.max_input_pixels:
            raise ValueError("max_batch_pixels must not be below max_input_pixels")


@dataclass(frozen=True)
class CropInput:
    crop_id: str
    png_bytes: bytes

    def __post_init__(self) -> None:
        if type(self.crop_id) is not str or not _SAFE_CROP_ID.fullmatch(self.crop_id):
            raise ValueError("crop_id contains unsafe characters")
        if type(self.png_bytes) is not bytes or not self.png_bytes:
            raise ValueError("png_bytes must be non-empty immutable bytes")


@dataclass(frozen=True)
class EnhancedCrop:
    crop_id: str
    source_sha256: str
    output_sha256: str
    png_bytes: bytes
    width: int
    height: int
    mode: str
    backend: EnhancementBackend
    recipe: str = RECIPE_ID
    gamma: float = GAMMA_DARK
    dpi: int = 300
    status: EnhancementStatus = EnhancementStatus.COMPLETE

    def __post_init__(self) -> None:
        if type(self.crop_id) is not str or not _SAFE_CROP_ID.fullmatch(self.crop_id):
            raise ValueError("crop_id contains unsafe characters")
        for name, value in (
            ("source_sha256", self.source_sha256),
            ("output_sha256", self.output_sha256),
        ):
            if (
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if type(self.png_bytes) is not bytes or not self.png_bytes.startswith(PNG_SIGNATURE):
            raise ValueError("enhanced crop payload must be immutable PNG bytes")
        if type(self.width) is not int or type(self.height) is not int:
            raise ValueError("enhanced crop dimensions must be integers")
        if self.width < 1 or self.height < 1:
            raise ValueError("enhanced crop dimensions must be positive")
        if self.mode != "L":
            raise ValueError("enhanced crop mode must be L")
        if not isinstance(self.backend, EnhancementBackend):
            raise ValueError("enhanced crop backend is invalid")
        if self.recipe != RECIPE_ID or self.gamma != GAMMA_DARK:
            raise ValueError("enhanced crop recipe is not the frozen winner")
        if type(self.dpi) is not int or not 1 <= self.dpi <= 2_400:
            raise ValueError("enhanced crop DPI is invalid")
        if self.status is not EnhancementStatus.COMPLETE:
            raise ValueError("enhanced crop status is invalid")
        if hashlib.sha256(self.png_bytes).hexdigest() != self.output_sha256:
            raise ValueError("enhanced crop output digest disagrees with PNG bytes")
        try:
            with Image.open(io.BytesIO(self.png_bytes)) as opened:
                embedded_dpi = opened.info.get("dpi")
                if (
                    opened.format != "PNG"
                    or opened.size != (self.width, self.height)
                    or opened.mode != self.mode
                    or getattr(opened, "n_frames", 1) != 1
                    or not isinstance(embedded_dpi, tuple)
                    or len(embedded_dpi) != 2
                    or any(
                        not isinstance(value, (int, float)) or abs(float(value) - self.dpi) > 0.01
                        for value in embedded_dpi
                    )
                ):
                    raise ValueError("enhanced crop metadata disagrees with its PNG payload")
                opened.verify()
        except ValueError:
            raise
        except (OSError, UnidentifiedImageError, SyntaxError) as exc:
            raise ValueError("enhanced crop payload is not a valid PNG") from exc


@dataclass(frozen=True)
class _CropHeader:
    item: CropInput
    width: int
    height: int
    source_sha256: str


@dataclass(frozen=True)
class _DecodedCrop:
    header: _CropHeader
    rgb: np.ndarray

    @property
    def item(self) -> CropInput:
        return self.header.item

    @property
    def width(self) -> int:
        return self.header.width

    @property
    def height(self) -> int:
        return self.header.height

    @property
    def source_sha256(self) -> str:
        return self.header.source_sha256


def normalize_dark_small_text_for_ocr(
    png_bytes: bytes,
    *,
    dpi: int,
    max_input_pixels: int,
) -> bytes | None:
    """Normalize sparse high-contrast dark crops without changing geometry.

    Height-only upscaling can enlarge an already legible UI/table x-height
    until narrow punctuation merges into neighbouring digits.  This gate uses
    image evidence rather than crop names: dark background, strong contrast,
    sparse foreground, and a robust connected-component height.  Dense dark
    text and every light crop remain on the existing adapter path.

    Raw RGB/RGBA crops receive the frozen Kornia gamma-dark recipe.  A
    grayscale payload is already a possible output of that recipe, so it is
    retained byte-for-byte to avoid applying gamma twice.  Geometry is always
    source scale; this function replaces one OCR payload and never schedules
    another recognition call.
    """

    if type(png_bytes) is not bytes or not png_bytes:
        raise CropEnhancementInvariantError(
            "small-text normalization requires immutable PNG bytes"
        )
    if type(dpi) is not int or not 1 <= dpi <= 2_400:
        raise ValueError("small-text normalization DPI is invalid")
    if type(max_input_pixels) is not int or max_input_pixels < 1:
        raise ValueError("small-text normalization pixel limit must be positive")
    try:
        with Image.open(io.BytesIO(png_bytes)) as opened:
            if (
                opened.format != "PNG"
                or getattr(opened, "n_frames", 1) != 1
            ):
                raise CropEnhancementInvariantError(
                    "small-text normalization requires one PNG frame"
                )
            opened.load()
            width, height = opened.size
            if (
                width < 1
                or height < 1
                or width * height > max_input_pixels
            ):
                return None
            source_mode = opened.mode
            if source_mode in {"RGBA", "LA"} or "transparency" in opened.info:
                rgba = opened.convert("RGBA")
                flattened = Image.new("RGBA", opened.size, "white")
                try:
                    flattened.alpha_composite(rgba)
                    grayscale_image = flattened.convert("L")
                finally:
                    rgba.close()
                    flattened.close()
            else:
                grayscale_image = opened.convert("L")
            try:
                grayscale = np.array(
                    grayscale_image,
                    dtype=np.uint8,
                    copy=True,
                )
            finally:
                grayscale_image.close()
    except CropEnhancementInvariantError:
        raise
    except (OSError, UnidentifiedImageError, SyntaxError, ValueError) as exc:
        raise CropEnhancementInvariantError(
            "small-text normalization input is not a valid PNG"
        ) from exc

    background = float(np.median(grayscale))
    if background > _DARK_BACKGROUND_MAX:
        return None
    _threshold, foreground = _otsu_foreground(grayscale)
    contrast = float(np.percentile(grayscale, 99.0)) - background
    foreground_ratio = float(np.mean(foreground))
    if (
        contrast < _DARK_TEXT_CONTRAST_MIN
        or not (
            _DARK_TEXT_FOREGROUND_MIN
            <= foreground_ratio
            <= _DARK_TEXT_FOREGROUND_MAX
        )
    ):
        return None

    maximum_component_width = min(80, max(2, width // 3))
    component_heights = tuple(
        int(component_height)
        for (
            component_width,
            component_height,
            component_area,
        ) in _foreground_component_stats(foreground)
        if (
            2 <= component_width <= maximum_component_width
            and 3 <= component_height <= min(40, height)
            and 3 <= component_area <= 1_000
            and component_width * component_height <= 7 * component_area
        )
    )
    if len(component_heights) < _DARK_TEXT_COMPONENTS_MIN:
        return None
    x_height = float(np.percentile(component_heights, 75.0))
    if not _DARK_TEXT_X_HEIGHT_MIN <= x_height <= _DARK_TEXT_X_HEIGHT_MAX:
        return None

    if source_mode == "L":
        return png_bytes
    config = CropEnhancementConfig(
        max_input_pixels=max_input_pixels,
        max_batch_pixels=max_input_pixels,
        dpi=dpi,
    )
    return GammaDarkCropEnhancer(config).enhance(
        CropInput("tesseract-dark-small-text", png_bytes)
    ).png_bytes


class GammaDarkCropEnhancer:
    """Generate an optional gamma candidate while leaving source bytes intact.

    Candidate selection is deliberately outside this stage.  The raw crop and
    this output must both reach Stage 2; this recipe is not a default policy.
    """

    def __init__(self, config: CropEnhancementConfig | None = None) -> None:
        if config is not None and not isinstance(config, CropEnhancementConfig):
            raise TypeError("config must be a CropEnhancementConfig")
        self.config = config or CropEnhancementConfig()

    def enhance(self, item: CropInput) -> EnhancedCrop:
        if not isinstance(item, CropInput):
            raise CropEnhancementInvariantError("item must be a CropInput")
        return self.enhance_many((item,))[0]

    def enhance_many(self, items: tuple[CropInput, ...]) -> tuple[EnhancedCrop, ...]:
        if type(items) is not tuple or any(not isinstance(item, CropInput) for item in items):
            raise CropEnhancementInvariantError("items must be an immutable CropInput tuple")
        if len(items) > self.config.max_batch_items:
            raise CropEnhancementLimitError(
                f"crop batch item count exceeds configured limit {self.config.max_batch_items}"
            )
        crop_ids = tuple(item.crop_id for item in items)
        if len(crop_ids) != len(set(crop_ids)):
            raise CropEnhancementInvariantError("crop identifiers must be unique")
        if not items:
            return ()

        # Inspect every immutable header before allocating even one decoded image.
        # This makes an over-budget batch fail atomically and bounds peak memory.
        headers = tuple(self._preflight(item) for item in items)
        batch_pixels = sum(item.width * item.height for item in headers)
        if batch_pixels > self.config.max_batch_pixels:
            raise CropEnhancementLimitError(f"crop batch pixels exceed configured limit {self.config.max_batch_pixels}")
        backend = self._resolve_backend()
        decoded = tuple(self._decode(header) for header in headers)
        if backend is EnhancementBackend.TORCH_CUDA:
            enhanced = self._enhance_torch_cuda(decoded)
        else:
            enhanced = tuple(self._enhance_numpy(item.rgb) for item in decoded)
        return tuple(self._encode(item, pixels, backend=backend) for item, pixels in zip(decoded, enhanced))

    def _preflight(self, item: CropInput) -> _CropHeader:
        payload = item.png_bytes
        if len(payload) > self.config.max_input_bytes:
            raise CropEnhancementLimitError(
                f"crop {item.crop_id} exceeds input byte limit {self.config.max_input_bytes}"
            )
        if not payload.startswith(PNG_SIGNATURE):
            raise CropEnhancementInvariantError(f"crop {item.crop_id} is not a PNG")
        try:
            with Image.open(io.BytesIO(payload)) as opened:
                if opened.format != "PNG":
                    raise CropEnhancementInvariantError(f"crop {item.crop_id} is not a PNG")
                width, height = opened.size
                if width < 1 or height < 1 or width > self.config.max_dimension or height > self.config.max_dimension:
                    raise CropEnhancementLimitError(f"crop {item.crop_id} dimensions exceed configured limit")
                pixels = width * height
                if pixels > self.config.max_input_pixels:
                    raise CropEnhancementLimitError(
                        f"crop {item.crop_id} pixel count exceeds configured limit {self.config.max_input_pixels}"
                    )
                if getattr(opened, "n_frames", 1) != 1:
                    raise CropEnhancementInvariantError(f"crop {item.crop_id} must contain exactly one PNG frame")
                raw_exif = opened.info.get("exif")
                if raw_exif is None:
                    orientation = 1
                elif type(raw_exif) is not bytes:
                    raise CropEnhancementInvariantError(f"crop {item.crop_id} contains invalid EXIF metadata")
                else:
                    metadata = Image.Exif()
                    metadata.load(raw_exif)
                    orientation = metadata.get(274, 1)
                if orientation not in (None, 1):
                    raise CropEnhancementInvariantError(f"crop {item.crop_id} contains an unresolved orientation")
        except CropEnhancementLimitError:
            raise
        except CropEnhancementInvariantError:
            raise
        except (OSError, UnidentifiedImageError, SyntaxError, ValueError) as exc:
            raise CropEnhancementInvariantError(f"crop {item.crop_id} contains an invalid PNG") from exc
        return _CropHeader(
            item=item,
            width=width,
            height=height,
            source_sha256=hashlib.sha256(payload).hexdigest(),
        )

    def _decode(self, header: _CropHeader) -> _DecodedCrop:
        item = header.item
        try:
            with Image.open(io.BytesIO(item.png_bytes)) as opened:
                if opened.format != "PNG" or opened.size != (
                    header.width,
                    header.height,
                ):
                    raise CropEnhancementInvariantError(f"crop {item.crop_id} changed after PNG preflight")
                opened.load()
                if opened.mode in {"RGBA", "LA"} or "transparency" in opened.info:
                    rgba = opened.convert("RGBA")
                    rgb_image = Image.new("RGB", rgba.size, "white")
                    try:
                        rgb_image.paste(rgba, mask=rgba.getchannel("A"))
                    finally:
                        rgba.close()
                else:
                    rgb_image = opened.convert("RGB")
                try:
                    rgb = np.array(rgb_image, dtype=np.uint8, copy=True)
                finally:
                    rgb_image.close()
        except CropEnhancementLimitError:
            raise
        except CropEnhancementInvariantError:
            raise
        except (OSError, UnidentifiedImageError, SyntaxError, ValueError) as exc:
            raise CropEnhancementInvariantError(f"crop {item.crop_id} contains an invalid PNG") from exc
        if rgb.shape != (header.height, header.width, 3):
            raise CropEnhancementInvariantError(f"crop {item.crop_id} decoded to an unexpected shape")
        rgb.setflags(write=False)
        return _DecodedCrop(
            header=header,
            rgb=rgb,
        )

    @staticmethod
    def _enhance_numpy(rgb: np.ndarray) -> np.ndarray:
        source = rgb.astype(np.float32) / np.float32(255.0)
        red, green, blue = np.moveaxis(source, 2, 0)
        grayscale = red * np.float32(0.299)
        grayscale += green * np.float32(0.587)
        grayscale += blue * np.float32(0.114)
        np.power(grayscale, np.float32(GAMMA_DARK), out=grayscale)
        np.clip(grayscale, 0.0, 1.0, out=grayscale)
        return np.rint(grayscale * np.float32(255.0)).astype(np.uint8)

    def _resolve_backend(self) -> EnhancementBackend:
        requested = self.config.backend
        # AUTO is deliberately hardware-independent.  This one-pass recipe is
        # cheap in NumPy and canonical bytes must not depend on the host GPU.
        if requested in (EnhancementBackend.AUTO, EnhancementBackend.NUMPY):
            return EnhancementBackend.NUMPY
        try:
            torch = importlib.import_module("torch")
            cuda_available = bool(torch.cuda.is_available())
        except Exception as exc:
            raise CropEnhancementBackendError("torch CUDA backend initialization failed") from exc
        if cuda_available:
            return EnhancementBackend.TORCH_CUDA
        raise CropEnhancementBackendError("torch CUDA backend was requested but CUDA is unavailable")

    @staticmethod
    def _enhance_torch_cuda(decoded: tuple[_DecodedCrop, ...]) -> tuple[np.ndarray, ...]:
        try:
            torch = importlib.import_module("torch")
            grouped: dict[tuple[int, int], list[tuple[int, _DecodedCrop]]] = {}
            for index, item in enumerate(decoded):
                grouped.setdefault((item.height, item.width), []).append((index, item))
            outputs: list[np.ndarray | None] = [None] * len(decoded)
            with torch.inference_mode():
                for group in grouped.values():
                    arrays = np.stack(
                        [item.rgb for _, item in group],
                        axis=0,
                    ).astype(np.float32)
                    tensor = torch.from_numpy(arrays).permute(0, 3, 1, 2).to(device="cuda", dtype=torch.float32) / 255.0
                    weights = torch.tensor(
                        [0.299, 0.587, 0.114],
                        device=tensor.device,
                        dtype=tensor.dtype,
                    )
                    red, green, blue = tensor.unbind(dim=1)
                    grayscale = red * weights[0]
                    grayscale = torch.addcmul(grayscale, green, weights[1])
                    grayscale = torch.addcmul(grayscale, blue, weights[2])
                    values = (
                        grayscale.pow(GAMMA_DARK).clamp(0.0, 1.0).mul(255.0).round().to(dtype=torch.uint8).cpu().numpy()
                    )
                    for (index, _), value in zip(group, values):
                        outputs[index] = value
            if any(value is None for value in outputs):
                raise RuntimeError("CUDA batch did not produce every crop")
            gpu_outputs = tuple(value for value in outputs if value is not None)
            # CUDA math is allowed as an acceleration path only.  PNG bytes are
            # canonical across hosts, so resolve rare device rounding drift with
            # the frozen NumPy reference before publishing the stage boundary.
            canonical_outputs = tuple(GammaDarkCropEnhancer._enhance_numpy(item.rgb) for item in decoded)
            return tuple(
                gpu if np.array_equal(gpu, canonical) else canonical
                for gpu, canonical in zip(gpu_outputs, canonical_outputs)
            )
        except CropEnhancementBackendError:
            raise
        except Exception as exc:
            raise CropEnhancementBackendError("torch CUDA crop enhancement failed") from exc

    def _encode(
        self,
        item: _DecodedCrop,
        pixels: np.ndarray,
        *,
        backend: EnhancementBackend,
    ) -> EnhancedCrop:
        if pixels.shape != (item.height, item.width) or pixels.dtype != np.uint8:
            raise CropEnhancementInvariantError(f"crop {item.item.crop_id} enhancement changed its geometry or dtype")
        output = io.BytesIO()
        image = Image.fromarray(pixels, mode="L")
        try:
            image.save(
                output,
                format="PNG",
                compress_level=9,
                optimize=False,
                dpi=(self.config.dpi, self.config.dpi),
            )
        finally:
            image.close()
        payload = output.getvalue()
        if len(payload) > self.config.max_output_bytes:
            raise CropEnhancementLimitError(
                f"crop {item.item.crop_id} output exceeds configured byte limit {self.config.max_output_bytes}"
            )
        return EnhancedCrop(
            crop_id=item.item.crop_id,
            source_sha256=item.source_sha256,
            output_sha256=hashlib.sha256(payload).hexdigest(),
            png_bytes=payload,
            width=item.width,
            height=item.height,
            mode="L",
            backend=backend,
            dpi=self.config.dpi,
        )


__all__ = [
    "CANDIDATE_ROLE",
    "DARK_SMALL_TEXT_NORMALIZATION_ID",
    "CropEnhancementBackendError",
    "CropEnhancementConfig",
    "CropEnhancementInvariantError",
    "CropEnhancementLimitError",
    "CropInput",
    "EnhancedCrop",
    "EnhancementBackend",
    "EnhancementStatus",
    "GAMMA_DARK",
    "GammaDarkCropEnhancer",
    "RECIPE_ID",
    "normalize_dark_small_text_for_ocr",
]
