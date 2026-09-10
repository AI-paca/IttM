from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, UnidentifiedImageError

from app.sparse_pipeline.block_crops import BlockCropPair
from app.sparse_pipeline.block_planning import (
    BlockPlan,
    BlockPlanningMode,
    sparse_matrix_payload,
    sparse_matrix_sha256,
)
from app.sparse_pipeline.contracts import SparseSegmentMatrix
from app.sparse_pipeline.crop_enhancement import CropInput

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class BlockArtifactWriter:
    """Publish one auditable Stage 5 plan without replacing an existing run."""

    def write(
        self,
        root: Path,
        *,
        run_id: str,
        page: CropInput,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        matrix: SparseSegmentMatrix | None = None,
    ) -> Path:
        if not isinstance(root, Path):
            raise TypeError("root must be a Path")
        if type(run_id) is not str or not _SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError("run_id contains unsafe characters")
        if not isinstance(page, CropInput):
            raise ValueError("page must be a CropInput")
        if not isinstance(plan, BlockPlan):
            raise ValueError("plan must be a BlockPlan")
        if type(crops) is not tuple or any(not isinstance(item, BlockCropPair) for item in crops):
            raise ValueError("crops must be an immutable BlockCropPair tuple")
        expected_ids = tuple(item.block_id for item in plan.blocks)
        if tuple(item.block_id for item in crops) != expected_ids:
            raise ValueError("crop order must exactly match the block plan")
        for block, crop in zip(plan.blocks, crops):
            if crop.bbox != block.bbox or crop.segment_ids != block.segment_ids:
                raise ValueError(f"crop {crop.block_id} disagrees with its block plan")
            if any(
                segment_id not in plan.source_segment_ids or segment_id in block.segment_ids
                for segment_id in crop.masked_segment_ids
            ):
                raise ValueError(f"crop {crop.block_id} isolation scope is invalid")
        self._validate_matrix(plan=plan, matrix=matrix)
        self._validate_page_crops(page, plan=plan, crops=crops)

        root.mkdir(parents=True, exist_ok=True)
        run_dir = root / run_id
        try:
            run_dir.mkdir()
        except FileExistsError as exc:
            raise FileExistsError(f"debug run already exists: {run_dir}") from exc
        temporary_dir: Path | None = None
        try:
            temporary_dir = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=root))
            stage_dir = temporary_dir / "05-blocks"
            stage_dir.mkdir()
            self._write_stage(
                stage_dir,
                page=page,
                plan=plan,
                crops=crops,
                matrix=matrix,
            )
            stage_dir.rename(run_dir / "05-blocks")
            temporary_dir.rmdir()
            return run_dir
        except Exception:
            if temporary_dir is not None:
                shutil.rmtree(temporary_dir, ignore_errors=True)
            shutil.rmtree(run_dir, ignore_errors=True)
            raise

    @staticmethod
    def _validate_matrix(
        *,
        plan: BlockPlan,
        matrix: SparseSegmentMatrix | None,
    ) -> None:
        if plan.mode is not BlockPlanningMode.SPATIAL_2D:
            if matrix is not None and not isinstance(matrix, SparseSegmentMatrix):
                raise ValueError("matrix must be a SparseSegmentMatrix or None")
            return
        if not isinstance(matrix, SparseSegmentMatrix):
            raise ValueError("spatial block artifacts require the Stage 1 matrix")
        if (
            matrix.segment_ids() != frozenset(plan.source_segment_ids)
            or sparse_matrix_sha256(matrix) != plan.matrix_sha256
        ):
            raise ValueError("spatial block artifact matrix disagrees with its plan")

    @staticmethod
    def _validate_page_crops(
        page: CropInput,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
    ) -> None:
        try:
            with Image.open(io.BytesIO(page.png_bytes)) as opened:
                if opened.format != "PNG" or opened.size != plan.aligned_size or getattr(opened, "n_frames", 1) != 1:
                    raise ValueError("page PNG metadata disagrees with the block plan")
                opened.load()
                if opened.mode in {"RGBA", "LA"} or "transparency" in opened.info:
                    rgba = opened.convert("RGBA")
                    canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    try:
                        canvas.alpha_composite(rgba)
                        page_rgb = canvas.convert("RGB")
                    finally:
                        rgba.close()
                        canvas.close()
                else:
                    page_rgb = opened.convert("RGB")
        except ValueError:
            raise
        except (OSError, UnidentifiedImageError, SyntaxError) as exc:
            raise ValueError("page is not a valid PNG") from exc
        try:
            for crop in crops:
                expected = page_rgb.crop(crop.bbox.as_tuple())
                try:
                    if crop.isolation_mask_png is not None:
                        with Image.open(io.BytesIO(crop.isolation_mask_png)) as mask:
                            mask.load()
                            expected.paste((255, 255, 255), mask=mask)
                    with Image.open(io.BytesIO(crop.raw.png_bytes)) as raw:
                        raw.load()
                        actual = raw.convert("RGB")
                    try:
                        if ImageChops.difference(expected, actual).getbbox() is not None:
                            raise ValueError(f"raw crop {crop.block_id} is not derived from source page")
                    finally:
                        actual.close()
                finally:
                    expected.close()
        finally:
            page_rgb.close()

    @staticmethod
    def _write_stage(
        stage_dir: Path,
        *,
        page: CropInput,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        matrix: SparseSegmentMatrix | None,
    ) -> None:
        page_dir = stage_dir / "page"
        raw_dir = stage_dir / "raw"
        gamma_dir = stage_dir / "gamma"
        isolation_dir = stage_dir / "isolation-masks"
        page_dir.mkdir()
        raw_dir.mkdir()
        gamma_dir.mkdir()
        if any(crop.isolation_mask_png is not None for crop in crops):
            isolation_dir.mkdir()
        page_path = page_dir / "source.png"
        page_path.write_bytes(page.png_bytes)
        if matrix is not None and plan.mode is BlockPlanningMode.SPATIAL_2D:
            matrix_record = {
                "sha256": sparse_matrix_sha256(matrix),
                **sparse_matrix_payload(matrix),
            }
            (stage_dir / "matrix.json").write_text(
                json.dumps(
                    matrix_record,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

        entries: list[dict[str, object]] = []
        for block, crop in zip(plan.blocks, crops):
            raw_path = raw_dir / f"{block.block_id}.png"
            gamma_path = gamma_dir / f"{block.block_id}.png"
            raw_path.write_bytes(crop.raw.png_bytes)
            gamma_path.write_bytes(crop.gamma.png_bytes)
            isolation_path: str | None = None
            if crop.isolation_mask_png is not None:
                mask_path = isolation_dir / f"{block.block_id}.png"
                mask_path.write_bytes(crop.isolation_mask_png)
                isolation_path = f"isolation-masks/{block.block_id}.png"
            entries.append(
                {
                    "block_id": block.block_id,
                    "bbox": list(block.bbox.as_tuple()),
                    "core_segment_ids": list(block.core_segment_ids),
                    "segment_ids": list(block.segment_ids),
                    "context_segment_ids": list(block.context_segment_ids),
                    "object_ids": list(block.object_ids),
                    "scope_id": block.scope_id,
                    "raw": f"raw/{block.block_id}.png",
                    "gamma": f"gamma/{block.block_id}.png",
                    "isolation_mask": isolation_path,
                    "masked_segment_ids": list(crop.masked_segment_ids),
                    "raw_crop_id": crop.raw.crop_id,
                    "gamma_crop_id": crop.gamma.crop_id,
                    "gamma_recipe": crop.gamma.recipe,
                    "gamma_backend": crop.gamma.backend.value,
                    "dpi": crop.gamma.dpi,
                    "raw_sha256": hashlib.sha256(crop.raw.png_bytes).hexdigest(),
                    "gamma_sha256": crop.gamma.output_sha256,
                    "gamma_source_sha256": crop.gamma.source_sha256,
                    "width": block.bbox.width,
                    "height": block.bbox.height,
                }
            )
        (stage_dir / "blocks.jsonl").write_text(
            "".join(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n" for entry in entries),
            encoding="utf-8",
        )
        algebra_entries = [
            {
                "first_block_id": item.first_block_id,
                "second_block_id": item.second_block_id,
                "intersection_segment_ids": list(item.intersection_segment_ids),
                "union_segment_ids": list(item.union_segment_ids),
                "xor_segment_ids": list(item.xor_segment_ids),
                "first_only_segment_ids": list(item.first_only_segment_ids),
                "second_only_segment_ids": list(item.second_only_segment_ids),
            }
            for item in plan.adjacent_algebra
        ]
        (stage_dir / "algebra.jsonl").write_text(
            "".join(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n" for entry in algebra_entries),
            encoding="utf-8",
        )
        membership_entries = [
            {
                "unit_id": item.unit_id,
                "kind": item.kind.value,
                "segment_ids": list(item.segment_ids),
                "block_ids": list(item.block_ids),
                "scope_id": item.scope_id,
            }
            for item in plan.membership_units
        ]
        if membership_entries:
            (stage_dir / "membership-units.jsonl").write_text(
                "".join(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n" for entry in membership_entries),
                encoding="utf-8",
            )
        full_width = all(item.bbox.left == 0 and item.bbox.right == plan.aligned_size[0] for item in plan.blocks)
        overlap_connected = BlockArtifactWriter._scope_overlap_connected(plan)
        diagnostics = (
            "stage=5 overlapping-blocks\n"
            "status=complete\n"
            f"planning_mode={plan.mode.value}\n"
            f"blocks={len(plan.blocks)}\n"
            f"adjacent_pairs={len(plan.adjacent_algebra)}\n"
            f"membership_units={len(plan.membership_units)}\n"
            f"matrix_sha256={plan.matrix_sha256 or 'not-applicable'}\n"
            "core_partition_exact=true\n"
            f"adjacent_overlap={str(overlap_connected).lower()}\n"
            f"full_width={str(full_width).lower()}\n"
            "raw_gamma_pairs=true\n"
            "selection_deferred_to_stage2=true\n"
        )
        (stage_dir / "diagnostics.txt").write_text(diagnostics, encoding="utf-8")
        manifest = {
            "semantic_stage": 5,
            "execution_step": 5,
            "stage_name": "overlapping-blocks",
            "status": plan.status.value,
            "planning_mode": plan.mode.value,
            "aligned_size": list(plan.aligned_size),
            "source_page": "page/source.png",
            "source_page_sha256": hashlib.sha256(page.png_bytes).hexdigest(),
            "source_segment_ids": list(plan.source_segment_ids),
            "blocks": len(plan.blocks),
            "adjacent_pairs": len(plan.adjacent_algebra),
            "membership_units": membership_entries,
            "matrix_sha256": plan.matrix_sha256,
            "order": [item.block_id for item in plan.blocks],
            "selection_stage": 2,
            "selection_deferred_to_stage2": True,
            "items": entries,
            "adjacent_algebra": algebra_entries,
            "diagnostics": list(plan.diagnostics),
            "invariants": {
                "core_partition_exact": True,
                "adjacent_overlap": overlap_connected,
                "full_width": full_width,
                "raw_gamma_pairs": True,
                "source_page_preserved": True,
            },
        }
        (stage_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _scope_overlap_connected(plan: BlockPlan) -> bool:
        if not plan.blocks:
            return True
        if all(item.scope_id is None for item in plan.blocks):
            return len(plan.blocks) <= 1 or len(plan.adjacent_algebra) >= len(plan.blocks) - 1
        by_scope: dict[str, list[int]] = {}
        for index, block in enumerate(plan.blocks):
            if block.scope_id is None:
                return False
            by_scope.setdefault(block.scope_id, []).append(index)
        for indexes in by_scope.values():
            if len(indexes) <= 1:
                continue
            reached = {indexes[0]}
            pending = [indexes[0]]
            while pending:
                current = pending.pop()
                current_members = set(plan.blocks[current].segment_ids)
                for neighbour in indexes:
                    if neighbour in reached:
                        continue
                    if current_members.intersection(plan.blocks[neighbour].segment_ids):
                        reached.add(neighbour)
                        pending.append(neighbour)
            if reached != set(indexes):
                return False
        return True


__all__ = ["BlockArtifactWriter"]
