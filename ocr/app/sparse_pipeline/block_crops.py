from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

import numpy as np
from PIL import Image, UnidentifiedImageError

from app.sparse_pipeline.block_planning import BlockPlan, BlockPlanningMode
from app.sparse_pipeline.contracts import Box
from app.sparse_pipeline.crop_enhancement import (
    CropEnhancementBackendError,
    CropEnhancementConfig,
    CropEnhancementInvariantError,
    CropEnhancementLimitError,
    CropInput,
    EnhancedCrop,
    EnhancementBackend,
    GammaDarkCropEnhancer,
    PNG_SIGNATURE,
)


class BlockCropInvariantError(ValueError):
    """Raised when a page or block plan violates the immutable crop boundary."""


class BlockCropLimitError(RuntimeError):
    """Raised before page decode or candidate generation would exceed limits."""


@dataclass(frozen=True)
class BlockCropConfig:
    max_page_bytes: int = 64 * 1024 * 1024
    max_page_pixels: int = 80_000_000
    max_dimension: int = 32_768
    max_blocks: int = 100_000
    max_block_pixels: int = 16_000_000
    max_total_crop_pixels: int = 64_000_000
    max_crop_bytes: int = 64 * 1024 * 1024
    max_total_output_bytes: int = 256 * 1024 * 1024
    max_batch_items: int = 256
    max_batch_pixels: int = 64_000_000
    dpi: int = 300
    enhancement_backend: EnhancementBackend = EnhancementBackend.AUTO

    def __post_init__(self) -> None:
        limits = (
            self.max_page_bytes,
            self.max_page_pixels,
            self.max_dimension,
            self.max_blocks,
            self.max_block_pixels,
            self.max_total_crop_pixels,
            self.max_crop_bytes,
            self.max_total_output_bytes,
            self.max_batch_items,
            self.max_batch_pixels,
            self.dpi,
        )
        if any(type(value) is not int or value < 1 for value in limits):
            raise ValueError("block crop limits must be positive integers")
        if not 1 <= self.dpi <= 2_400:
            raise ValueError("dpi must be between 1 and 2400")
        if self.max_batch_pixels < self.max_block_pixels:
            raise ValueError("max_batch_pixels must not be below max_block_pixels")
        if self.max_total_crop_pixels < self.max_block_pixels:
            raise ValueError("max_total_crop_pixels must not be below max_block_pixels")
        if self.max_total_output_bytes < self.max_crop_bytes:
            raise ValueError("max_total_output_bytes must not be below max_crop_bytes")
        if not isinstance(self.enhancement_backend, EnhancementBackend):
            raise ValueError("enhancement_backend must be an EnhancementBackend")


@dataclass(frozen=True)
class BlockCropPair:
    block_id: str
    bbox: Box
    segment_ids: tuple[str, ...]
    raw: CropInput
    gamma: EnhancedCrop | None
    isolation_mask_png: bytes | None = None
    masked_segment_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.block_id) is not str or not self.block_id:
            raise ValueError("block_id must not be empty")
        if not isinstance(self.bbox, Box):
            raise ValueError("block crop bbox must be a Box")
        if (
            type(self.segment_ids) is not tuple
            or not self.segment_ids
            or any(type(item) is not str or not item for item in self.segment_ids)
            or len(self.segment_ids) != len(set(self.segment_ids))
        ):
            raise ValueError("block crop segment IDs must be a unique immutable tuple")
        if not isinstance(self.raw, CropInput) or (
            self.gamma is not None
            and not isinstance(self.gamma, EnhancedCrop)
        ):
            raise ValueError("block crop candidates have invalid types")
        if (
            type(self.masked_segment_ids) is not tuple
            or any(
                type(segment_id) is not str or not segment_id
                for segment_id in self.masked_segment_ids
            )
            or len(self.masked_segment_ids)
            != len(set(self.masked_segment_ids))
        ):
            raise ValueError(
                "masked segment IDs must be a unique immutable string tuple"
            )
        if (self.isolation_mask_png is None) != (not self.masked_segment_ids):
            raise ValueError(
                "an isolation mask and masked segment IDs must occur together"
            )
        if self.raw.crop_id != f"{self.block_id}-raw":
            raise ValueError("raw crop identifier disagrees with its block")
        if self.gamma is not None:
            if self.gamma.crop_id != f"{self.block_id}-gamma":
                raise ValueError("gamma crop identifier disagrees with its block")
            if (
                hashlib.sha256(self.raw.png_bytes).hexdigest()
                != self.gamma.source_sha256
            ):
                raise ValueError("gamma candidate was not derived from the raw crop")
            if (self.gamma.width, self.gamma.height) != (
                self.bbox.width,
                self.bbox.height,
            ):
                raise ValueError("gamma candidate geometry disagrees with its block")
        try:
            with Image.open(io.BytesIO(self.raw.png_bytes)) as opened:
                embedded_dpi = opened.info.get("dpi")
                if (
                    opened.format != "PNG"
                    or opened.mode != "RGB"
                    or opened.size != (self.bbox.width, self.bbox.height)
                    or getattr(opened, "n_frames", 1) != 1
                    or not isinstance(embedded_dpi, tuple)
                    or len(embedded_dpi) != 2
                    or any(
                        not isinstance(value, (int, float))
                        or abs(
                            float(value)
                            - (self.gamma.dpi if self.gamma is not None else 300)
                        )
                        > 0.01
                        for value in embedded_dpi
                    )
                ):
                    raise ValueError("raw candidate metadata disagrees with its block")
                opened.verify()
        except ValueError:
            raise
        except (OSError, UnidentifiedImageError, SyntaxError) as exc:
            raise ValueError("raw candidate is not a valid PNG") from exc
        if self.isolation_mask_png is not None:
            try:
                with Image.open(io.BytesIO(self.isolation_mask_png)) as mask:
                    mask.load()
                    extrema = mask.getextrema()
                    histogram = mask.histogram()
                    if (
                        mask.format != "PNG"
                        or mask.mode != "L"
                        or mask.size != (self.bbox.width, self.bbox.height)
                        or getattr(mask, "n_frames", 1) != 1
                        or extrema != (0, 255)
                        or any(histogram[1:255])
                    ):
                        raise ValueError(
                            "isolation mask metadata or values are invalid"
                        )
                    mask_values = np.asarray(mask, dtype=np.uint8) == 255
                with Image.open(io.BytesIO(self.raw.png_bytes)) as raw:
                    raw.load()
                    raw_values = np.array(
                        raw.convert("RGB"),
                        dtype=np.uint8,
                        copy=True,
                    )
                if np.any(raw_values[mask_values] != 255):
                    raise ValueError(
                        "isolation mask pixels must be white in the raw crop"
                    )
            except ValueError:
                raise
            except (OSError, UnidentifiedImageError, SyntaxError) as exc:
                raise ValueError("isolation mask is not a valid PNG") from exc


class BlockCropper:
    """Cut raw RGB blocks and generate optional gamma candidates in batches."""

    def __init__(self, config: BlockCropConfig | None = None) -> None:
        if config is not None and not isinstance(config, BlockCropConfig):
            raise TypeError("config must be a BlockCropConfig")
        self.config = config or BlockCropConfig()

    def crop(
        self,
        page: CropInput,
        *,
        aligned_size: tuple[int, int],
        plan: BlockPlan,
        ownership: np.ndarray | None = None,
        ownership_segment_ids: tuple[str, ...] | None = None,
        isolation_source: tuple[BlockCropPair, ...] | None = None,
    ) -> tuple[BlockCropPair, ...]:
        crops, _ = self.crop_with_rgb_sha256(
            page,
            aligned_size=aligned_size,
            plan=plan,
            ownership=ownership,
            ownership_segment_ids=ownership_segment_ids,
            isolation_source=isolation_source,
        )
        return crops

    def crop_with_rgb_sha256(
        self,
        page: CropInput,
        *,
        aligned_size: tuple[int, int],
        plan: BlockPlan,
        ownership: np.ndarray | None = None,
        ownership_segment_ids: tuple[str, ...] | None = None,
        isolation_source: tuple[BlockCropPair, ...] | None = None,
    ) -> tuple[tuple[BlockCropPair, ...], str]:
        """Cut blocks and bind them to canonical decoded RGB pixels."""
        if not isinstance(page, CropInput):
            raise BlockCropInvariantError("page must be a CropInput")
        if type(aligned_size) is not tuple:
            raise BlockCropInvariantError("aligned_size must be an immutable tuple")
        if not isinstance(plan, BlockPlan):
            raise BlockCropInvariantError("plan must be a BlockPlan")
        if aligned_size != plan.aligned_size:
            raise BlockCropInvariantError("page and block plan aligned sizes disagree")
        if len(plan.blocks) > self.config.max_blocks:
            raise BlockCropLimitError(f"block count exceeds configured limit {self.config.max_blocks}")
        if any(block.bbox.area > self.config.max_block_pixels for block in plan.blocks):
            raise BlockCropLimitError(f"block pixels exceed configured limit {self.config.max_block_pixels}")
        total_crop_pixels = sum(block.bbox.area for block in plan.blocks)
        if total_crop_pixels > self.config.max_total_crop_pixels:
            raise BlockCropLimitError(
                f"aggregate crop pixels exceed configured limit {self.config.max_total_crop_pixels}"
            )
        foreign_by_block = self._validate_exact_spatial_membership(
            aligned_size=aligned_size,
            plan=plan,
            ownership=ownership,
            ownership_segment_ids=ownership_segment_ids,
            isolation_source=isolation_source,
        )
        image = self._decode_page(page, aligned_size=aligned_size)
        try:
            rgb_sha256 = self._rgb_sha256(image)
            return (
                self._crop_batches(
                    image,
                    plan=plan,
                    ownership=ownership,
                    ownership_segment_ids=ownership_segment_ids,
                    foreign_by_block=foreign_by_block,
                    isolation_source=isolation_source,
                ),
                rgb_sha256,
            )
        finally:
            image.close()

    @staticmethod
    def _validate_exact_spatial_membership(
        *,
        aligned_size: tuple[int, int],
        plan: BlockPlan,
        ownership: np.ndarray | None,
        ownership_segment_ids: tuple[str, ...] | None,
        isolation_source: tuple[BlockCropPair, ...] | None = None,
    ) -> dict[str, tuple[str, ...]]:
        """Certify literal Stage 1 ownership visible inside every spatial crop.

        Bounding-box closure in the planner is deliberately conservative, but
        it is not the source-of-truth for foreground pixels.  When the Stage 1
        ownership raster is supplied, this gate proves that the physical crop
        exposes every declared member and no undeclared (including
        cross-object) segment before an OCR request can be queued.

        The evidence pair remains optional for backwards-compatible debug and
        full-width callers.  The production spatial runtime always supplies
        it.  Supplying only half of the pair is rejected rather than silently
        downgrading the check.
        """

        if (ownership is None) != (ownership_segment_ids is None):
            raise BlockCropInvariantError(
                "ownership raster and segment order must be supplied together"
            )
        if isolation_source is not None:
            if (
                type(isolation_source) is not tuple
                or len(isolation_source) != len(plan.blocks)
                or any(
                    not isinstance(item, BlockCropPair)
                    for item in isolation_source
                )
            ):
                raise BlockCropInvariantError(
                    "isolation replay must follow the complete block plan"
                )
            for block, crop in zip(plan.blocks, isolation_source):
                if (
                    crop.block_id != block.block_id
                    or crop.bbox != block.bbox
                    or crop.segment_ids != block.segment_ids
                    or any(
                        segment_id not in plan.source_segment_ids
                        or segment_id in block.segment_ids
                        for segment_id in crop.masked_segment_ids
                    )
                ):
                    raise BlockCropInvariantError(
                        "isolation replay disagrees with the block plan"
                    )
            if plan.mode is BlockPlanningMode.SPATIAL_2D and ownership is None:
                raise BlockCropInvariantError(
                    "spatial isolation replay requires the Stage 1 ownership raster"
                )
            if plan.mode is not BlockPlanningMode.SPATIAL_2D and any(
                crop.isolation_mask_png is not None for crop in isolation_source
            ):
                raise BlockCropInvariantError(
                    "non-spatial isolation replay cannot contain ownership masks"
                )
        if ownership is None:
            return {}
        assert ownership_segment_ids is not None
        if not isinstance(ownership, np.ndarray):
            raise BlockCropInvariantError("ownership must be a NumPy array")
        width, height = aligned_size
        if ownership.ndim != 2 or ownership.shape != (height, width):
            raise BlockCropInvariantError(
                "ownership raster geometry disagrees with the aligned page"
            )
        if ownership.dtype.kind != "i":
            raise BlockCropInvariantError(
                "ownership raster must use signed integer labels"
            )
        if (
            type(ownership_segment_ids) is not tuple
            or any(
                type(segment_id) is not str or not segment_id
                for segment_id in ownership_segment_ids
            )
            or len(ownership_segment_ids) != len(set(ownership_segment_ids))
        ):
            raise BlockCropInvariantError(
                "ownership segment order must be a unique immutable string tuple"
            )
        if (
            len(ownership_segment_ids) != len(plan.source_segment_ids)
            or set(ownership_segment_ids) != set(plan.source_segment_ids)
        ):
            raise BlockCropInvariantError(
                "ownership segment order disagrees with the block plan"
            )
        minimum = int(ownership.min())
        maximum = int(ownership.max())
        if minimum < -1 or maximum >= len(ownership_segment_ids):
            raise BlockCropInvariantError(
                "ownership raster contains an unknown segment label"
            )
        if plan.mode is not BlockPlanningMode.SPATIAL_2D:
            return {}

        foreign_by_block: dict[str, tuple[str, ...]] = {}
        for block in plan.blocks:
            visible_labels = np.unique(
                ownership[
                    block.bbox.top : block.bbox.bottom,
                    block.bbox.left : block.bbox.right,
                ]
            )
            visible_ids = {
                ownership_segment_ids[int(label)]
                for label in visible_labels
                if int(label) >= 0
            }
            declared_ids = set(block.segment_ids)
            missing = tuple(
                segment_id
                for segment_id in block.segment_ids
                if segment_id not in visible_ids
            )
            foreign = tuple(
                segment_id
                for segment_id in ownership_segment_ids
                if segment_id in visible_ids and segment_id not in declared_ids
            )
            if missing:
                raise BlockCropInvariantError(
                    f"spatial crop {block.block_id} has missing ownership "
                    f"members: missing={missing[:8]!r}"
                )
            foreign_by_block[block.block_id] = foreign
        if isolation_source is not None:
            replay_by_block = {
                crop.block_id: crop for crop in isolation_source
            }
            label_by_segment = {
                segment_id: label
                for label, segment_id in enumerate(ownership_segment_ids)
            }
            for block in plan.blocks:
                crop = replay_by_block[block.block_id]
                expected_ids = foreign_by_block[block.block_id]
                if crop.masked_segment_ids != expected_ids:
                    raise BlockCropInvariantError(
                        f"isolation replay {block.block_id} masked segment IDs "
                        "disagree with Stage 1 ownership"
                    )
                expected_mask = BlockCropper._ownership_isolation_mask(
                    ownership,
                    block.bbox,
                    tuple(label_by_segment[item] for item in expected_ids),
                )
                if crop.isolation_mask_png != expected_mask:
                    raise BlockCropInvariantError(
                        f"isolation replay {block.block_id} mask disagrees "
                        "with Stage 1 ownership"
                    )
        return foreign_by_block

    @staticmethod
    def _rgb_sha256(image: Image.Image) -> str:
        """Hash row-major RGB pixels without materialising the whole page twice."""
        digest = hashlib.sha256()
        stripe_height = 512
        for top in range(0, image.height, stripe_height):
            stripe = image.crop(
                (0, top, image.width, min(image.height, top + stripe_height))
            )
            try:
                digest.update(stripe.tobytes())
            finally:
                stripe.close()
        return digest.hexdigest()

    def _crop_batches(
        self,
        image: Image.Image,
        *,
        plan: BlockPlan,
        ownership: np.ndarray | None = None,
        ownership_segment_ids: tuple[str, ...] | None = None,
        foreign_by_block: dict[str, tuple[str, ...]] | None = None,
        isolation_source: tuple[BlockCropPair, ...] | None = None,
    ) -> tuple[BlockCropPair, ...]:
        enhancer = self._enhancer()
        outputs: list[BlockCropPair] = []
        batch_blocks = []
        batch_raw: list[CropInput] = []
        batch_masks: list[bytes | None] = []
        batch_masked_ids: list[tuple[str, ...]] = []
        batch_pixels = 0
        output_bytes = 0

        def flush() -> None:
            nonlocal batch_blocks, batch_raw, batch_masks
            nonlocal batch_masked_ids, batch_pixels, output_bytes
            if not batch_blocks:
                return
            gamma_inputs = tuple(
                CropInput(f"{block.block_id}-gamma", raw.png_bytes) for block, raw in zip(batch_blocks, batch_raw)
            )
            try:
                gamma_outputs = enhancer.enhance_many(gamma_inputs)
            except CropEnhancementLimitError as exc:
                raise BlockCropLimitError("gamma candidate exceeded crop limits") from exc
            except CropEnhancementInvariantError as exc:
                raise BlockCropInvariantError("raw crop violated gamma input invariants") from exc
            except CropEnhancementBackendError:
                raise
            for block, raw, gamma, mask_png, masked_ids in zip(
                batch_blocks,
                batch_raw,
                gamma_outputs,
                batch_masks,
                batch_masked_ids,
            ):
                output_bytes += (
                    len(raw.png_bytes)
                    + len(gamma.png_bytes)
                    + (len(mask_png) if mask_png is not None else 0)
                )
                if output_bytes > self.config.max_total_output_bytes:
                    raise BlockCropLimitError(
                        f"aggregate crop output bytes exceed configured limit {self.config.max_total_output_bytes}"
                    )
                outputs.append(
                    BlockCropPair(
                        block_id=block.block_id,
                        bbox=block.bbox,
                        segment_ids=block.segment_ids,
                        raw=raw,
                        gamma=gamma,
                        isolation_mask_png=mask_png,
                        masked_segment_ids=masked_ids,
                    )
                )
            batch_blocks = []
            batch_raw = []
            batch_masks = []
            batch_masked_ids = []
            batch_pixels = 0

        replay_by_block = (
            {item.block_id: item for item in isolation_source}
            if isolation_source is not None
            else {}
        )
        label_by_segment = (
            {
                segment_id: label
                for label, segment_id in enumerate(ownership_segment_ids)
            }
            if ownership_segment_ids is not None
            else {}
        )
        for block in plan.blocks:
            pixels = block.bbox.area
            would_overflow = (
                len(batch_blocks) >= self.config.max_batch_items or batch_pixels + pixels > self.config.max_batch_pixels
            )
            if batch_blocks and would_overflow:
                flush()
            replay = (
                replay_by_block.get(block.block_id)
                if ownership is None
                else None
            )
            if replay is not None:
                mask_png = replay.isolation_mask_png
                masked_ids = replay.masked_segment_ids
            else:
                masked_ids = (foreign_by_block or {}).get(
                    block.block_id,
                    (),
                )
                mask_png = self._ownership_isolation_mask(
                    ownership,
                    block.bbox,
                    tuple(label_by_segment[item] for item in masked_ids),
                )
            batch_blocks.append(block)
            batch_raw.append(
                self._raw_crop(
                    image,
                    block.block_id,
                    block.bbox,
                    isolation_mask_png=mask_png,
                )
            )
            batch_masks.append(mask_png)
            batch_masked_ids.append(masked_ids)
            batch_pixels += pixels
        flush()
        if len(outputs) != len(plan.blocks):
            raise BlockCropInvariantError("candidate batching lost a block")
        return tuple(outputs)

    def _decode_page(
        self,
        page: CropInput,
        *,
        aligned_size: tuple[int, int],
    ) -> Image.Image:
        payload = page.png_bytes
        if len(payload) > self.config.max_page_bytes:
            raise BlockCropLimitError(f"page exceeds configured byte limit {self.config.max_page_bytes}")
        if not payload.startswith(PNG_SIGNATURE):
            raise BlockCropInvariantError("page is not a PNG")
        try:
            with Image.open(io.BytesIO(payload)) as opened:
                if opened.format != "PNG":
                    raise BlockCropInvariantError("page is not a PNG")
                width, height = opened.size
                if (width, height) != aligned_size:
                    raise BlockCropInvariantError("decoded page size disagrees with aligned geometry")
                if (
                    width > self.config.max_dimension
                    or height > self.config.max_dimension
                    or width * height > self.config.max_page_pixels
                ):
                    raise BlockCropLimitError("page dimensions exceed configured limits")
                if getattr(opened, "n_frames", 1) != 1:
                    raise BlockCropInvariantError("page must contain exactly one PNG frame")
                raw_exif = opened.info.get("exif")
                if raw_exif is None:
                    orientation = 1
                elif type(raw_exif) is not bytes:
                    raise BlockCropInvariantError("page contains invalid EXIF metadata")
                else:
                    metadata = Image.Exif()
                    metadata.load(raw_exif)
                    orientation = metadata.get(274, 1)
                if orientation not in (None, 1):
                    raise BlockCropInvariantError("page contains an unresolved orientation")
                opened.load()
                if opened.mode in {"RGBA", "LA"} or "transparency" in opened.info:
                    rgba = opened.convert("RGBA")
                    canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    try:
                        canvas.alpha_composite(rgba)
                        rgb = canvas.convert("RGB")
                    finally:
                        rgba.close()
                        canvas.close()
                else:
                    rgb = opened.convert("RGB")
        except BlockCropLimitError:
            raise
        except BlockCropInvariantError:
            raise
        except (OSError, UnidentifiedImageError, SyntaxError, ValueError) as exc:
            raise BlockCropInvariantError("page contains an invalid PNG") from exc
        return rgb

    @staticmethod
    def _ownership_isolation_mask(
        ownership: np.ndarray | None,
        bbox: Box,
        foreign_labels: tuple[int, ...],
    ) -> bytes | None:
        if not foreign_labels:
            return None
        if ownership is None:
            raise BlockCropInvariantError(
                "foreign ownership isolation requires the Stage 1 raster"
            )
        local = ownership[bbox.top : bbox.bottom, bbox.left : bbox.right]
        mask_values = np.isin(local, foreign_labels)
        if not bool(mask_values.any()):
            raise BlockCropInvariantError(
                "declared foreign ownership has no physical crop pixels"
            )
        mask_image = Image.fromarray(mask_values.astype(np.uint8) * 255, mode="L")
        output = io.BytesIO()
        try:
            mask_image.save(
                output,
                format="PNG",
                compress_level=9,
                optimize=False,
            )
        finally:
            mask_image.close()
        return output.getvalue()

    def _raw_crop(
        self,
        image: Image.Image,
        block_id: str,
        bbox: Box,
        *,
        isolation_mask_png: bytes | None = None,
    ) -> CropInput:
        cropped = image.crop(bbox.as_tuple())
        output = io.BytesIO()
        try:
            if isolation_mask_png is not None:
                with Image.open(io.BytesIO(isolation_mask_png)) as opened_mask:
                    opened_mask.load()
                    cropped.paste((255, 255, 255), mask=opened_mask)
            cropped.save(
                output,
                format="PNG",
                compress_level=9,
                optimize=False,
                dpi=(self.config.dpi, self.config.dpi),
            )
        finally:
            cropped.close()
        payload = output.getvalue()
        if len(payload) > self.config.max_crop_bytes:
            raise BlockCropLimitError(f"raw crop {block_id} exceeds byte limit {self.config.max_crop_bytes}")
        return CropInput(f"{block_id}-raw", payload)

    def _enhancer(self) -> GammaDarkCropEnhancer:
        return GammaDarkCropEnhancer(
            CropEnhancementConfig(
                backend=self.config.enhancement_backend,
                max_input_bytes=self.config.max_crop_bytes,
                max_input_pixels=self.config.max_block_pixels,
                max_dimension=self.config.max_dimension,
                max_batch_items=self.config.max_batch_items,
                max_batch_pixels=self.config.max_batch_pixels,
                max_output_bytes=self.config.max_crop_bytes,
                dpi=self.config.dpi,
            )
        )


__all__ = [
    "BlockCropConfig",
    "BlockCropInvariantError",
    "BlockCropLimitError",
    "BlockCropPair",
    "BlockCropper",
]
