from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

from app.sparse_pipeline.atomic_publish import rename_no_replace
from app.sparse_pipeline.block_crops import BlockCropConfig, BlockCropPair
from app.sparse_pipeline.block_planning import BlockPlan, BlockPlanningConfig
from app.sparse_pipeline.contracts import GeometryResult
from app.sparse_pipeline.crop_enhancement import CropInput
from app.sparse_pipeline.document_assembly import (
    AssemblyStatus,
    DocumentAssembler,
    DocumentAssemblyConfig,
    DocumentAssemblyResult,
    EvidenceSlice,
    ObjectTextAssembly,
    SegmentTextAssembly,
    StructuralUnit,
)
from app.sparse_pipeline.object_reconstruction import (
    ObjectReconstructionConfig,
    ObjectReconstructionResult,
)
from app.sparse_pipeline.ocr_fusion import OcrFusionConfig, OcrFusionResult
from app.sparse_pipeline.ocr_queue import OcrQueueResult


class DocumentArtifactWriter:
    """Atomically publish one immutable Stage 7 document evidence tree."""

    def write(
        self,
        root: Path,
        result: DocumentAssemblyResult,
        *,
        page: CropInput,
        geometry: GeometryResult,
        objects: ObjectReconstructionResult,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        queue: OcrQueueResult,
        fusion: OcrFusionResult,
        ownership: np.ndarray | None = None,
        ownership_segment_ids: tuple[str, ...] | None = None,
        assembly_config: DocumentAssemblyConfig | None = None,
        object_config: ObjectReconstructionConfig | None = None,
        planning_config: BlockPlanningConfig | None = None,
        crop_config: BlockCropConfig | None = None,
        fusion_config: OcrFusionConfig | None = None,
    ) -> Path:
        if not isinstance(root, Path):
            raise TypeError("root must be a Path")
        if root.name in {"", ".", ".."}:
            raise ValueError("run root must name one unambiguous new directory")
        if not isinstance(result, DocumentAssemblyResult):
            raise TypeError("result must be a DocumentAssemblyResult")
        if root.exists():
            raise FileExistsError(f"debug run already exists: {root}")
        expected = DocumentAssembler(assembly_config).assemble(
            page=page,
            geometry=geometry,
            objects=objects,
            plan=plan,
            crops=crops,
            queue=queue,
            fusion=fusion,
            ownership=ownership,
            ownership_segment_ids=ownership_segment_ids,
            object_config=object_config,
            planning_config=planning_config,
            crop_config=crop_config,
            fusion_config=fusion_config,
        )
        if result != expected:
            raise ValueError(
                "document result was not assembled from the supplied stage evidence"
            )

        parent = root.parent
        parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{root.name}.partial-", dir=parent)
        )
        try:
            self._write_stage(temporary / "07-document", result)
            rename_no_replace(temporary, root)
            return root
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    @staticmethod
    def _write_stage(stage_dir: Path, result: DocumentAssemblyResult) -> None:
        candidate_dir = stage_dir / "candidate"
        records_dir = stage_dir / "records"
        object_text_dir = stage_dir / "objects"
        segment_text_dir = stage_dir / "segments"
        candidate_dir.mkdir(parents=True)
        records_dir.mkdir()
        object_text_dir.mkdir()
        segment_text_dir.mkdir()

        DocumentArtifactWriter._write_text(
            candidate_dir / "document.txt",
            result.candidate_text,
        )
        DocumentArtifactWriter._write_text(
            candidate_dir / "document.md",
            result.candidate_markdown,
        )
        if result.status is AssemblyStatus.COMPLETE:
            certified_dir = stage_dir / "certified"
            certified_dir.mkdir()
            assert result.text is not None and result.markdown is not None
            DocumentArtifactWriter._write_text(
                certified_dir / "document.txt",
                result.text,
            )
            DocumentArtifactWriter._write_text(
                certified_dir / "document.md",
                result.markdown,
            )

        record_sets: tuple[tuple[str, tuple[object, ...], type[object]], ...] = (
            (
                "evidence-slices",
                tuple(result.evidence_slices),
                EvidenceSlice,
            ),
            ("segments", tuple(result.segments), SegmentTextAssembly),
            (
                "structural-units",
                tuple(result.structural_units),
                StructuralUnit,
            ),
            ("objects", tuple(result.objects), ObjectTextAssembly),
        )
        for name, values, record_type in record_sets:
            DocumentArtifactWriter._write_jsonl(
                records_dir / f"{name}.jsonl",
                values,
            )
            DocumentArtifactWriter._write_tsv(
                records_dir / f"{name}.tsv",
                values,
                record_type=record_type,
            )

        for index, item in enumerate(result.objects):
            item_dir = object_text_dir / f"object-{index:08d}"
            item_dir.mkdir()
            DocumentArtifactWriter._write_text(
                item_dir / "candidate.txt", item.candidate_text
            )
            DocumentArtifactWriter._write_text(
                item_dir / "candidate.md", item.candidate_markdown
            )
            if item.text is not None and item.markdown is not None:
                DocumentArtifactWriter._write_text(
                    item_dir / "certified.txt", item.text
                )
                DocumentArtifactWriter._write_text(
                    item_dir / "certified.md", item.markdown
                )
        for index, item in enumerate(result.segments):
            item_dir = segment_text_dir / f"segment-{index:08d}"
            item_dir.mkdir()
            DocumentArtifactWriter._write_text(
                item_dir / "candidate.txt", item.candidate_text
            )
            if item.text is not None:
                DocumentArtifactWriter._write_text(
                    item_dir / "certified.txt", item.text
                )

        diagnostics = "\n".join(result.diagnostics)
        DocumentArtifactWriter._write_text(
            stage_dir / "diagnostics.txt",
            diagnostics + ("\n" if diagnostics else ""),
        )
        payloads = tuple(
            path
            for path in sorted(stage_dir.rglob("*"))
            if path.is_file() and path.name != "manifest.json"
        )
        manifest = {
            "semantic_stage": 7,
            "execution_step": 7,
            "stage_name": "document-assembly",
            "status": result.status.value,
            "certified": result.status is AssemblyStatus.COMPLETE,
            "source_segment_ids": list(result.source_segment_ids),
            "source_object_ids": list(result.source_object_ids),
            "source_block_ids": list(result.source_block_ids),
            "diagnostics": list(result.diagnostics),
            "files": [
                {
                    "path": path.relative_to(stage_dir).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": DocumentArtifactWriter._sha256(path),
                }
                for path in payloads
            ],
        }
        DocumentArtifactWriter._write_json(stage_dir / "manifest.json", manifest)

    @classmethod
    def _json_value(cls, value: object) -> Any:
        if isinstance(value, Enum):
            return cls._json_value(value.value)
        if is_dataclass(value) and not isinstance(value, type):
            return {
                field.name: cls._json_value(getattr(value, field.name))
                for field in fields(value)
            }
        if isinstance(value, Mapping):
            return {
                str(key): cls._json_value(item) for key, item in value.items()
            }
        if isinstance(value, (tuple, list)):
            return [cls._json_value(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise TypeError(f"unsupported artifact value: {type(value).__name__}")

    @classmethod
    def _write_json(cls, path: Path, value: object) -> None:
        path.write_text(
            json.dumps(
                cls._json_value(value),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    @classmethod
    def _write_jsonl(cls, path: Path, values: Iterable[object]) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            for value in values:
                handle.write(
                    json.dumps(
                        cls._json_value(value),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                handle.write("\n")

    @classmethod
    def _write_tsv(
        cls,
        path: Path,
        values: tuple[object, ...],
        *,
        record_type: type[object],
    ) -> None:
        field_names = tuple(field.name for field in fields(record_type))
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=field_names,
                dialect="excel-tab",
                lineterminator="\n",
            )
            writer.writeheader()
            for value in values:
                writer.writerow(
                    {
                        name: cls._tsv_value(getattr(value, name))
                        for name in field_names
                    }
                )

    @classmethod
    def _tsv_value(cls, value: object) -> str | int | float:
        converted = cls._json_value(value)
        if converted is None:
            return ""
        if isinstance(converted, bool):
            return "true" if converted else "false"
        if isinstance(converted, (str, int, float)):
            return converted
        return json.dumps(
            converted,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        path.write_text(value, encoding="utf-8")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


__all__ = ["DocumentArtifactWriter"]
