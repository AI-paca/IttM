from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.sparse_pipeline.crop_enhancement import (
    CANDIDATE_ROLE,
    CropInput,
    EnhancedCrop,
)

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class CropEnhancementArtifactWriter:
    """Atomically publish auditable source/output PNG pairs for stage 4."""

    def write(
        self,
        root: Path,
        *,
        run_id: str,
        inputs: tuple[CropInput, ...],
        results: tuple[EnhancedCrop, ...],
    ) -> Path:
        if not isinstance(root, Path):
            raise TypeError("root must be a Path")
        if type(run_id) is not str or not _SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError("run_id contains unsafe characters")
        if type(inputs) is not tuple or any(not isinstance(item, CropInput) for item in inputs):
            raise ValueError("inputs must be an immutable CropInput tuple")
        if type(results) is not tuple or any(not isinstance(item, EnhancedCrop) for item in results):
            raise ValueError("results must be an immutable EnhancedCrop tuple")
        input_ids = tuple(item.crop_id for item in inputs)
        result_ids = tuple(item.crop_id for item in results)
        if input_ids != result_ids or len(input_ids) != len(set(input_ids)):
            raise ValueError("enhancement inputs and results must have identical order")
        if len({result.backend for result in results}) > 1:
            raise ValueError("one enhancement run cannot mix execution backends")
        if len({result.dpi for result in results}) > 1:
            raise ValueError("one enhancement run cannot mix output DPI values")
        for source, result in zip(inputs, results):
            if hashlib.sha256(source.png_bytes).hexdigest() != result.source_sha256:
                raise ValueError(f"source digest disagrees for crop {source.crop_id}")
            if self._source_size(source) != (result.width, result.height):
                raise ValueError(f"enhancement changed geometry for crop {source.crop_id}")

        root.mkdir(parents=True, exist_ok=True)
        run_dir = root / run_id
        try:
            # mkdir is the portable atomic no-replace primitive for directories.
            # It prevents rename(2) from silently replacing a concurrently
            # reserved but still-empty destination directory.
            run_dir.mkdir()
        except FileExistsError as exc:
            raise FileExistsError(f"debug run already exists: {run_dir}") from exc
        temporary_dir: Path | None = None
        try:
            temporary_dir = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=root))
            stage_dir = temporary_dir / "04-enhancement"
            stage_dir.mkdir()
            self._write_stage(stage_dir, inputs=inputs, results=results)
            stage_dir.rename(run_dir / "04-enhancement")
            temporary_dir.rmdir()
            return run_dir
        except Exception:
            if temporary_dir is not None:
                shutil.rmtree(temporary_dir, ignore_errors=True)
            shutil.rmtree(run_dir, ignore_errors=True)
            raise

    @staticmethod
    def _source_size(source: CropInput) -> tuple[int, int]:
        try:
            with Image.open(io.BytesIO(source.png_bytes)) as opened:
                if opened.format != "PNG" or getattr(opened, "n_frames", 1) != 1:
                    raise ValueError(f"source crop {source.crop_id} is not a PNG")
                size = opened.size
                opened.verify()
        except ValueError:
            raise
        except (OSError, UnidentifiedImageError, SyntaxError) as exc:
            raise ValueError(f"source crop {source.crop_id} is not a valid PNG") from exc
        return size

    @staticmethod
    def _write_stage(
        stage_dir: Path,
        *,
        inputs: tuple[CropInput, ...],
        results: tuple[EnhancedCrop, ...],
    ) -> None:
        source_dir = stage_dir / "source"
        output_dir = stage_dir / "output"
        source_dir.mkdir()
        output_dir.mkdir()
        entries: list[dict[str, object]] = []
        for source, result in zip(inputs, results):
            source_path = source_dir / f"{source.crop_id}.png"
            output_path = output_dir / f"{source.crop_id}.png"
            source_path.write_bytes(source.png_bytes)
            output_path.write_bytes(result.png_bytes)
            entries.append(
                {
                    "crop_id": result.crop_id,
                    "source_sha256": result.source_sha256,
                    "output_sha256": result.output_sha256,
                    "width": result.width,
                    "height": result.height,
                    "mode": result.mode,
                    "backend": result.backend.value,
                    "recipe": result.recipe,
                    "gamma": result.gamma,
                    "dpi": result.dpi,
                    "status": result.status.value,
                    "candidate_role": CANDIDATE_ROLE,
                    "source": f"source/{source.crop_id}.png",
                    "output": f"output/{source.crop_id}.png",
                }
            )

        json_lines = "".join(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n" for entry in entries)
        (stage_dir / "crops.jsonl").write_text(json_lines, encoding="utf-8")
        diagnostics = (
            "stage=4 crop-enhancement\n"
            f"status=complete\n"
            f"crops={len(entries)}\n"
            "same_geometry=true\n"
            "source_output_digests=true\n"
            "raw_source_retained=true\n"
            "selection_deferred_to_stage2=true\n"
            "candidate_only=true\n"
            "raw_source_preserved=true\n"
        )
        (stage_dir / "diagnostics.txt").write_text(diagnostics, encoding="utf-8")
        manifest = {
            "semantic_stage": 4,
            "execution_step": 4,
            "stage_name": "crop-enhancement",
            "status": "complete",
            "crops": len(entries),
            "order": [entry["crop_id"] for entry in entries],
            "recipe": entries[0]["recipe"] if entries else None,
            "backend": entries[0]["backend"] if entries else None,
            "candidate_role": CANDIDATE_ROLE,
            "candidate_only": True,
            "selection_deferred_to_stage2": True,
            "selection_stage": 2,
            "unconditional_default": False,
            "items": entries,
            "invariants": {
                "same_geometry": True,
                "deterministic_order": True,
                "source_output_digests": True,
                "raw_source_retained": True,
                "raw_source_preserved": True,
            },
        }
        (stage_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )


__all__ = ["CropEnhancementArtifactWriter"]
