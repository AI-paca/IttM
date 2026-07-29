from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Mapping

import numpy as np
from PIL import Image, ImageChops, ImageDraw

from app.sparse_pipeline.atomic_publish import rename_no_replace
from app.sparse_pipeline.block_artifacts import BlockArtifactWriter
from app.sparse_pipeline.block_crops import BlockCropPair
from app.sparse_pipeline.block_planning import RecognitionBlock
from app.sparse_pipeline.contact_sheets import write_paired_contact_sheets
from app.sparse_pipeline.control_artifacts import ControlArtifactWriter
from app.sparse_pipeline.crop_enhancement import CropInput, EnhancedCrop
from app.sparse_pipeline.document_artifacts import DocumentArtifactWriter
from app.sparse_pipeline.document_assembly import (
    AssemblyStatus,
    DocumentAssembler,
    DocumentAssemblyConfig,
    DocumentAssemblyResult,
)
from app.sparse_pipeline.enhancement_artifacts import (
    CropEnhancementArtifactWriter,
)
from app.sparse_pipeline.geometry import GeometryBundle
from app.sparse_pipeline.geometry_artifacts import GeometryArtifactWriter
from app.sparse_pipeline.object_artifacts import ObjectArtifactWriter
from app.sparse_pipeline.ocr_artifacts import OcrArtifactWriter
from app.sparse_pipeline.ocr_fusion import OcrFusionStatus
from app.sparse_pipeline.ocr_queue import OcrQueueStatus
from app.sparse_pipeline.pipeline_control import PIPELINE_ORDER
from app.sparse_pipeline.pipeline_evidence import SparsePipelineEvidence
from app.sparse_pipeline.recursive_control import RunOutcome, RunStatus

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_STAGE_DIRECTORIES = (
    "03-control",
    "01-geometry",
    "06-objects",
    "04-enhancement",
    "05-blocks",
    "02-ocr",
    "07-document",
)
_STAGE_NAMES = (
    "recursive-control",
    "geometry-sparse-matrix",
    "object-reconstruction",
    "page-enhancement-candidate",
    "overlapping-context-blocks",
    "ocr-evidence-fusion",
    "document-assembly",
)
_STATUS_VOCABULARY = ("COMPLETE", "UNRESOLVED", "FAILED")


class TutorialArtifactWriter:
    """Publish one immutable, self-contained tutorial evidence tree.

    Every stage is rendered below one temporary directory.  Only after all
    provenance checks, ordinary evidence writers and tutorial visualizations
    succeed is the directory made visible with an atomic no-replace rename.
    """

    _MAX_RENDERED_PAIR_VISUALS = 8

    def write(
        self,
        root: Path,
        *,
        run_id: str,
        control: RunOutcome[str],
        geometry: GeometryBundle,
        evidence: SparsePipelineEvidence,
        document: DocumentAssemblyResult,
        assembly_config: DocumentAssemblyConfig | None = None,
        provenance: Mapping[str, object] | None = None,
    ) -> Path:
        stage4_input = self._validate(
            root=root,
            run_id=run_id,
            control=control,
            geometry=geometry,
            evidence=evidence,
            document=document,
            assembly_config=assembly_config,
        )
        destination = root / run_id
        root.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(f"debug run already exists: {destination}")

        temporary = Path(
            tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=root)
        )
        try:
            self._write_control(temporary / "03-control", control)

            geometry_stage = temporary / "01-geometry"
            geometry_stage.mkdir()
            GeometryArtifactWriter()._write_bundle(geometry_stage, geometry)

            object_stage = temporary / "06-objects"
            object_stage.mkdir()
            ObjectArtifactWriter()._write_result(object_stage, evidence.objects)

            enhancement_stage = temporary / "04-enhancement"
            enhancement_stage.mkdir()
            assert evidence.stage4 is not None
            CropEnhancementArtifactWriter._write_stage(
                enhancement_stage,
                inputs=(stage4_input,),
                results=(evidence.stage4,),
            )
            self._write_enhancement_visuals(
                enhancement_stage,
                source=stage4_input,
                result=evidence.stage4,
            )

            block_stage = temporary / "05-blocks"
            block_stage.mkdir()
            BlockArtifactWriter._write_stage(
                block_stage,
                page=evidence.page,
                plan=evidence.plan,
                crops=evidence.crops,
                matrix=evidence.geometry.matrix,
            )
            self._write_block_visuals(
                block_stage,
                geometry=geometry,
                evidence=evidence,
            )
            self._write_block_crop_gallery(block_stage, evidence=evidence)

            ocr_stage = temporary / "02-ocr"
            ocr_stage.mkdir()
            OcrArtifactWriter._write_stage(
                ocr_stage,
                plan=evidence.plan,
                segments=evidence.geometry.segmentation.segments,
                crops=evidence.crops,
                queue=evidence.queue,
                fusion=evidence.fusion,
                matrix=evidence.geometry.matrix,
            )
            self._write_ocr_visuals(ocr_stage, evidence=evidence)

            document_stage = temporary / "07-document"
            document_stage.mkdir()
            DocumentArtifactWriter._write_stage(document_stage, document)

            self._write_debug_layout(
                temporary,
                run_id=run_id,
                geometry=geometry,
                evidence=evidence,
                document=document,
                provenance=provenance,
            )

            statuses = self._stage_statuses(evidence=evidence, document=document)
            self._write_index(
                temporary,
                run_id=run_id,
                statuses=statuses,
                evidence=evidence,
            )
            rename_no_replace(temporary, destination)
            return destination
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    @classmethod
    def _write_debug_layout(
        cls,
        root: Path,
        *,
        run_id: str,
        geometry: GeometryBundle,
        evidence: SparsePipelineEvidence,
        document: DocumentAssemblyResult,
        provenance: Mapping[str, object] | None,
    ) -> None:
        """Publish the object-centric view used for manual v20 debugging.

        Stage directories remain the canonical evidence.  This view copies the
        exact Stage 1 ownership crops and Stage 5 OCR inputs into the object
        that owns them, so a reviewer never has to infer a returned segment or
        block from a rectangle drawn over the source page.
        """

        cls._write_root_matrix(root, geometry=geometry)
        cls._write_root_document(root, document=document)
        cls._write_object_views(
            root,
            geometry=geometry,
            evidence=evidence,
            document=document,
        )
        cls._write_stage_logs(root)

        supplied = dict(provenance or {})
        try:
            json.dumps(supplied, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("provenance must be JSON serializable") from exc
        provenance_record = {
            "schema": "sparse-v20-debug-provenance-v1",
            "run_id": run_id,
            "source_rgb_sha256": hashlib.sha256(
                memoryview(np.ascontiguousarray(geometry.source_rgb))
            ).hexdigest(),
            "aligned_rgb_sha256": evidence.geometry.aligned_rgb_sha256,
            "aligned_page_png_sha256": hashlib.sha256(
                evidence.page.png_bytes
            ).hexdigest(),
            "execution_order": list(PIPELINE_ORDER),
            "provided": supplied,
        }
        cls._write_json(root / "provenance.json", provenance_record)

    @classmethod
    def _write_root_matrix(
        cls,
        root: Path,
        *,
        geometry: GeometryBundle,
    ) -> None:
        stage = root / "01-geometry"
        shutil.copyfile(stage / "matrix.json", root / "sparse-matrix.json")
        shutil.copyfile(stage / "matrix.txt", root / "sparse-matrix.txt")
        # Ownership is a literal Stage 1 product.  Unlike an overlay, every
        # coloured pixel has an actual segment/rule owner.
        shutil.copyfile(
            stage / "ownership.png",
            root / "sparse-matrix-ownership.png",
        )

        matrix = geometry.result.matrix
        rows = max(1, len(matrix.rows))
        columns = max(1, len(matrix.columns))
        maximum_pixels = 16_000_000
        exact = rows * columns <= maximum_pixels and max(rows, columns) <= 4096
        if exact:
            display_rows, display_columns = rows, columns
        else:
            scale = min(1.0, 2048.0 / max(rows, columns))
            display_rows = max(1, round(rows * scale))
            display_columns = max(1, round(columns * scale))
        canvas = np.full((display_rows, display_columns, 3), 255, dtype=np.uint8)
        occupied: set[tuple[int, int]] = set()
        for cell in matrix.cells:
            y = min(display_rows - 1, cell.row * display_rows // rows)
            x = min(display_columns - 1, cell.column * display_columns // columns)
            coordinate = (y, x)
            if coordinate in occupied:
                canvas[y, x] = (0, 0, 0)
                continue
            occupied.add(coordinate)
            digest = hashlib.sha256(cell.segment_id.encode("utf-8")).digest()
            canvas[y, x] = (
                32 + digest[0] // 2,
                32 + digest[1] // 2,
                32 + digest[2] // 2,
            )
        logical = Image.fromarray(canvas, mode="RGB")
        try:
            # Keep tiny matrices readable without altering their cell values.
            largest = max(display_rows, display_columns)
            factor = min(16, max(1, 640 // largest))
            if factor > 1:
                rendered = logical.resize(
                    (display_columns * factor, display_rows * factor),
                    resample=Image.Resampling.NEAREST,
                )
            else:
                rendered = logical.copy()
            try:
                rendered.save(root / "sparse-matrix.png", format="PNG")
            finally:
                rendered.close()
        finally:
            logical.close()
        (root / "sparse-matrix.tsv").write_text(
            "row\tcolumn\tsegment_id\n"
            + "".join(
                f"{cell.row}\t{cell.column}\t{cell.segment_id}\n"
                for cell in matrix.cells
            ),
            encoding="utf-8",
        )
        cls._write_json(
            root / "sparse-matrix-image.json",
            {
                "logical_shape": [rows, columns],
                "display_shape": [display_rows, display_columns],
                "exact_one_pixel_per_cell": exact,
                "definition": (
                    "white=empty sparse coordinate; colour=one segment; "
                    "black=multiple occupied coordinates collapsed only in "
                    "the display image"
                ),
                "exact_records": "sparse-matrix.tsv",
                "literal_pixel_ownership": "sparse-matrix-ownership.png",
            },
        )

    @staticmethod
    def _write_root_document(
        root: Path,
        *,
        document: DocumentAssemblyResult,
    ) -> None:
        text = document.text if document.text is not None else document.candidate_text
        markdown = (
            document.markdown
            if document.markdown is not None
            else document.candidate_markdown
        )
        (root / "document.txt").write_text(
            text,
            encoding="utf-8",
        )
        (root / "document.md").write_text(
            markdown,
            encoding="utf-8",
        )
        TutorialArtifactWriter._write_json(
            root / "document.json",
            {
                "status": document.status.value,
                "certified": document.status is AssemblyStatus.COMPLETE,
                "source_object_ids": list(document.source_object_ids),
                "source_segment_ids": list(document.source_segment_ids),
                "source_block_ids": list(document.source_block_ids),
                "diagnostics": list(document.diagnostics),
            },
        )

    @classmethod
    def _write_object_views(
        cls,
        root: Path,
        *,
        geometry: GeometryBundle,
        evidence: SparsePipelineEvidence,
        document: DocumentAssemblyResult,
    ) -> None:
        objects_root = root / "objects"
        objects_root.mkdir()
        geometry_segments = {
            item.segment_id: item
            for item in evidence.geometry.segmentation.segments
        }
        matrix_cells: dict[str, list[list[int]]] = {
            item: [] for item in document.source_segment_ids
        }
        for cell in evidence.geometry.matrix.cells:
            matrix_cells[cell.segment_id].append([cell.row, cell.column])
        matrix_spans = {
            item.segment_id: {
                "row_start": item.row_start,
                "row_stop": item.row_stop,
                "column_start": item.column_start,
                "column_stop": item.column_stop,
            }
            for item in evidence.geometry.matrix.spans
        }
        segment_fusion = {
            item.segment_id: item for item in evidence.fusion.segments
        }
        segment_assembly = {
            item.segment_id: item for item in document.segments
        }
        document_objects = {item.object_id: item for item in document.objects}
        crop_by_block = {item.block_id: item for item in evidence.crops}
        owner_by_segment = {
            item.segment_id: item.object_id
            for item in evidence.objects.segment_ownership
        }
        ownership_order = tuple(
            item.segment_id
            for item in evidence.geometry.segmentation.segments
        )
        ownership_sha256 = (
            hashlib.sha256(
                memoryview(np.ascontiguousarray(geometry.ownership))
            ).hexdigest()
        )
        jobs_by_block: dict[str, list[object]] = {
            item.block_id: [] for item in evidence.plan.blocks
        }
        for job in evidence.queue.jobs:
            jobs_by_block.setdefault(job.block_id, []).append(job)
        observations_by_segment: dict[str, list[object]] = {
            item: [] for item in document.source_segment_ids
        }
        for observation in evidence.fusion.observations:
            observations_by_segment[observation.segment_id].append(observation)

        index_lines = [
            "# Object-local sparse v20 evidence",
            "",
            "Каждый object содержит literal Stage 1 segment crops и literal "
            "Stage 5 raw/enhanced block PNG. Overlay здесь не используется как "
            "доказательство.",
            "",
            "| order | object | kind | segments | blocks | result | debug |",
            "|---:|---|---|---:|---:|---|---|",
        ]
        object_manifest: list[dict[str, object]] = []
        for source_object in evidence.objects.objects:
            object_id = source_object.object_id
            assembled = document_objects[object_id]
            object_root = objects_root / object_id
            segment_root = object_root / "segments"
            block_root = object_root / "blocks"
            segment_root.mkdir(parents=True)
            block_root.mkdir()

            object_text = (
                assembled.text
                if assembled.text is not None
                else assembled.candidate_text
            )
            object_markdown = (
                assembled.markdown
                if assembled.markdown is not None
                else assembled.candidate_markdown
            )
            (object_root / "object.txt").write_text(
                object_text,
                encoding="utf-8",
            )
            (object_root / "object.md").write_text(
                object_markdown,
                encoding="utf-8",
            )

            object_blocks = tuple(
                block
                for block in evidence.plan.blocks
                if object_id in block.object_ids
                or bool(set(block.segment_ids) & set(source_object.segment_ids))
            )
            cls._write_json(
                object_root / "object.json",
                {
                    "object_id": object_id,
                    "reading_index": source_object.reading_index,
                    "kind": source_object.kind.value,
                    "bbox": list(source_object.bbox.as_tuple()),
                    "sparse_span": {
                        "row_start": source_object.row_start,
                        "row_stop": source_object.row_stop,
                        "column_start": source_object.column_start,
                        "column_stop": source_object.column_stop,
                    },
                    "confidence": float(source_object.confidence),
                    "segment_ids": list(source_object.segment_ids),
                    "block_ids": [item.block_id for item in object_blocks],
                    "assembly_status": assembled.status.value,
                    "certified": assembled.status is AssemblyStatus.COMPLETE,
                    "reasons": list(assembled.reasons),
                    "evidence": list(source_object.evidence),
                },
            )

            debug_lines = [
                f"# Debug `{object_id}`",
                "",
                f"kind=`{source_object.kind.value}`; status=`{assembled.status.value}`; "
                f"bbox=`{list(source_object.bbox.as_tuple())}`.",
                "",
                "## Segments",
                "",
            ]
            for segment_id in source_object.segment_ids:
                segment = geometry_segments[segment_id]
                fusion = segment_fusion[segment_id]
                assembly = segment_assembly[segment_id]
                raw_source = (
                    root
                    / "01-geometry"
                    / "segment-crops"
                    / "raw"
                    / f"{segment_id}.png"
                )
                isolated_source = (
                    root
                    / "01-geometry"
                    / "segment-crops"
                    / "isolated"
                    / f"{segment_id}.png"
                )
                raw_target = segment_root / f"{segment_id}.png"
                isolated_target = segment_root / f"{segment_id}.isolated.png"
                shutil.copyfile(raw_source, raw_target)
                shutil.copyfile(isolated_source, isolated_target)
                segment_text = (
                    assembly.text
                    if assembly.text is not None
                    else assembly.candidate_text
                )
                (segment_root / f"{segment_id}.txt").write_text(
                    segment_text,
                    encoding="utf-8",
                )
                cls._write_json(
                    segment_root / f"{segment_id}.json",
                    {
                        "segment_id": segment_id,
                        "object_id": object_id,
                        "bbox": list(segment.bbox.as_tuple()),
                        "source_bbox": list(segment.source_bbox.as_tuple()),
                        "kind": segment.kind.value,
                        "ink_pixels": segment.ink_pixels,
                        "sparse_cells": matrix_cells[segment_id],
                        "sparse_span": matrix_spans[segment_id],
                        "raw_png": f"{segment_id}.png",
                        "isolated_png": f"{segment_id}.isolated.png",
                        "raw_sha256": hashlib.sha256(
                            raw_target.read_bytes()
                        ).hexdigest(),
                        "isolated_sha256": hashlib.sha256(
                            isolated_target.read_bytes()
                        ).hexdigest(),
                        "selected_text": fusion.selected_text,
                        "selected_observation_id": fusion.selected_observation_id,
                        "selected_transform": (
                            fusion.selected_transform.value
                            if fusion.selected_transform is not None
                            else None
                        ),
                        "selected_lane_id": fusion.selected_lane_id,
                        "confidence": fusion.confidence,
                        "observation_count": fusion.observation_count,
                        "independent_context_count": fusion.independent_context_count,
                        "stability": fusion.stability,
                        "uncertainty_reasons": list(fusion.uncertainty_reasons),
                        "assembly_status": assembly.status.value,
                        "assembly_reasons": list(assembly.reasons),
                        "observations": [
                            {
                                "observation_id": item.observation_id,
                                "job_id": item.job_id,
                                "block_id": item.block_id,
                                "transform": item.transform.value,
                                "lane_id": item.lane_id,
                                "text": item.text,
                                "confidence": float(item.confidence),
                                "page_bboxes": [
                                    list(box.as_tuple()) for box in item.page_bboxes
                                ],
                            }
                            for item in observations_by_segment[segment_id]
                        ],
                    },
                )
                debug_lines.extend(
                    (
                        f"### `{segment_id}`",
                        "",
                        f"bbox=`{list(segment.bbox.as_tuple())}`; text=`{segment_text}`; "
                        f"status=`{assembly.status.value}`.",
                        "",
                        f"| ![{segment_id} raw](segments/{segment_id}.png) | "
                        f"![{segment_id} ownership](segments/{segment_id}.isolated.png) |",
                        "|---|---|",
                        "",
                    )
                )

            debug_lines.extend(("## Blocks", ""))
            for block in object_blocks:
                crop = crop_by_block[block.block_id]
                target = block_root / block.block_id
                target.mkdir()
                raw_path = target / "raw.png"
                enhanced_path = target / "enhanced.png"
                raw_path.write_bytes(crop.raw.png_bytes)
                enhanced_path.write_bytes(crop.gamma.png_bytes)
                isolation_mask_name = cls._write_block_isolation_evidence(
                    target,
                    crop=crop,
                    block=block,
                    object_id=object_id,
                    owner_by_segment=owner_by_segment,
                    ownership_sha256=ownership_sha256,
                    ownership_order=ownership_order,
                )
                algebra = tuple(
                    item
                    for item in evidence.plan.adjacent_algebra
                    if block.block_id
                    in (item.first_block_id, item.second_block_id)
                )
                local_segments = tuple(
                    item
                    for item in block.segment_ids
                    if item in source_object.segment_ids
                )
                cls._write_json(
                    target / "membership.json",
                    {
                        "block_id": block.block_id,
                        "bbox": list(block.bbox.as_tuple()),
                        "scope_id": block.scope_id,
                        "object_ids": list(block.object_ids),
                        "view_object_id": object_id,
                        "core_segment_ids": list(block.core_segment_ids),
                        "context_segment_ids": list(block.context_segment_ids),
                        "segment_ids": list(block.segment_ids),
                        "object_local_segment_ids": list(local_segments),
                        "foreign_segment_ids": [
                            item
                            for item in block.segment_ids
                            if item not in source_object.segment_ids
                        ],
                        "masked_segment_ids": list(crop.masked_segment_ids),
                        "isolation": "isolation.json",
                        "isolation_mask": isolation_mask_name,
                        "raw_sha256": hashlib.sha256(crop.raw.png_bytes).hexdigest(),
                        "enhanced_sha256": hashlib.sha256(
                            crop.gamma.png_bytes
                        ).hexdigest(),
                        "enhancement_recipe": crop.gamma.recipe,
                        "enhancement_backend": crop.gamma.backend.value,
                        "algebra": [
                            {
                                "first_block_id": item.first_block_id,
                                "second_block_id": item.second_block_id,
                                "intersection_segment_ids": list(
                                    item.intersection_segment_ids
                                ),
                                "union_segment_ids": list(item.union_segment_ids),
                                "xor_segment_ids": list(item.xor_segment_ids),
                            }
                            for item in algebra
                        ],
                    },
                )
                job_records: list[dict[str, object]] = []
                job_text: list[str] = []
                for job in jobs_by_block.get(block.block_id, []):
                    output = job.output
                    job_records.append(
                        {
                            "job_id": job.job_id,
                            "transform": job.transform.value,
                            "lane_id": job.lane_id,
                            "capability_id": job.capability_id,
                            "status": job.status.value,
                            "elapsed_seconds": float(job.elapsed_seconds),
                            "input_sha256": job.input_sha256,
                            "context_sha256": job.context_sha256,
                            "text": output.text if output is not None else None,
                            "geometry": (
                                output.geometry.value if output is not None else None
                            ),
                            "words": (
                                [
                                    {
                                        "text": word.text,
                                        "bbox": list(word.bbox.as_tuple()),
                                        "confidence": float(word.confidence),
                                    }
                                    for word in output.words
                                ]
                                if output is not None
                                else []
                            ),
                            "error_type": job.error_type,
                            "error_message": job.error_message,
                        }
                    )
                    job_text.extend(
                        (
                            f"[{job.job_id}] transform={job.transform.value} "
                            f"lane={job.lane_id} status={job.status.value}",
                            output.text if output is not None else "",
                            "",
                        )
                    )
                cls._write_json(target / "ocr.json", {"jobs": job_records})
                (target / "ocr.txt").write_text(
                    "\n".join(job_text), encoding="utf-8"
                )
                debug_lines.extend(
                    (
                        f"### `{block.block_id}`",
                        "",
                        f"members=`{list(block.segment_ids)}`; "
                        f"object-local=`{list(local_segments)}`; "
                        f"masked foreign=`{list(crop.masked_segment_ids)}`; "
                        "[isolation provenance]"
                        f"(blocks/{block.block_id}/isolation.json).",
                        "",
                        f"| ![{block.block_id} raw](blocks/{block.block_id}/raw.png) | "
                        f"![{block.block_id} enhanced](blocks/{block.block_id}/enhanced.png) |",
                        "|---|---|",
                        "",
                    )
                )
                if isolation_mask_name is not None:
                    debug_lines.extend(
                        (
                            "Isolation mask (white = removed before OCR):",
                            "",
                            f"![{block.block_id} isolation mask]"
                            f"(blocks/{block.block_id}/{isolation_mask_name})",
                            "",
                        )
                    )
            (object_root / "debag.md").write_text(
                "\n".join(debug_lines) + "\n", encoding="utf-8"
            )
            index_lines.append(
                f"| {source_object.reading_index} | `{object_id}` | "
                f"{source_object.kind.value} | {len(source_object.segment_ids)} | "
                f"{len(object_blocks)} | [object.md]({object_id}/object.md) | "
                f"[debag.md]({object_id}/debag.md) |"
            )
            object_manifest.append(
                {
                    "object_id": object_id,
                    "kind": source_object.kind.value,
                    "reading_index": source_object.reading_index,
                    "segments": len(source_object.segment_ids),
                    "blocks": len(object_blocks),
                    "directory": object_id,
                }
            )
        (objects_root / "index.md").write_text(
            "\n".join(index_lines) + "\n", encoding="utf-8"
        )
        cls._write_json(
            objects_root / "manifest.json",
            {
                "schema": "sparse-v20-object-debug-v1",
                "objects": object_manifest,
                "literal_crops_only": True,
                "overlay_is_evidence": False,
            },
        )

    @classmethod
    def _write_stage_logs(cls, root: Path) -> None:
        logs = root / "logs"
        logs.mkdir()
        table = ["execution_step\tsemantic_stage\tstage_name\tstatus\tmanifest"]
        for step, (stage_number, stage_name, directory) in enumerate(
            zip(PIPELINE_ORDER, _STAGE_NAMES, _STAGE_DIRECTORIES), start=1
        ):
            manifest_path = root / directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            status = str(manifest.get("status", "unknown"))
            log_name = f"{step:02d}-stage-{stage_number}-{stage_name}.log"
            lines = [
                f"execution_step={step}",
                f"semantic_stage={stage_number}",
                f"stage_name={stage_name}",
                f"status={status}",
                f"manifest=../{directory}/manifest.json",
                "",
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
                "",
            ]
            (logs / log_name).write_text("\n".join(lines), encoding="utf-8")
            table.append(
                f"{step}\t{stage_number}\t{stage_name}\t{status}\t"
                f"../{directory}/manifest.json"
            )
        (logs / "stages.tsv").write_text(
            "\n".join(table) + "\n", encoding="utf-8"
        )

    @classmethod
    def _validate(
        cls,
        *,
        root: Path,
        run_id: str,
        control: RunOutcome[str],
        geometry: GeometryBundle,
        evidence: SparsePipelineEvidence,
        document: DocumentAssemblyResult,
        assembly_config: DocumentAssemblyConfig | None,
    ) -> CropInput:
        if not isinstance(root, Path):
            raise TypeError("root must be a Path")
        if type(run_id) is not str or not _SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError("run_id contains unsafe characters")
        if not isinstance(control, RunOutcome):
            raise TypeError("control must be a RunOutcome")
        if not isinstance(geometry, GeometryBundle):
            raise TypeError("geometry must be a GeometryBundle")
        if not isinstance(evidence, SparsePipelineEvidence):
            raise TypeError("evidence must be SparsePipelineEvidence")
        if not isinstance(document, DocumentAssemblyResult):
            raise TypeError("document must be a DocumentAssemblyResult")
        if assembly_config is not None and not isinstance(
            assembly_config, DocumentAssemblyConfig
        ):
            raise TypeError("assembly_config must be a DocumentAssemblyConfig or None")

        cls._validate_control(control)
        cls._validate_geometry(geometry, evidence=evidence)
        if evidence.stage4 is None:
            raise ValueError("tutorial evidence requires the Stage 4 page candidate")

        expected_document = DocumentAssembler(assembly_config).assemble(
            page=evidence.page,
            geometry=evidence.geometry,
            objects=evidence.objects,
            plan=evidence.plan,
            crops=evidence.crops,
            queue=evidence.queue,
            fusion=evidence.fusion,
            ownership=geometry.ownership,
            ownership_segment_ids=tuple(
                segment.segment_id
                for segment in geometry.result.segmentation.segments
            ),
            object_config=evidence.object_config,
            planning_config=evidence.planning_config,
            crop_config=evidence.crop_config,
            fusion_config=evidence.fusion_config,
        )
        if document != expected_document:
            raise ValueError(
                "document result was not assembled from the supplied pipeline evidence"
            )

        # A page candidate may have a stage-specific crop ID.  Alias only the
        # identifier; immutable source bytes remain exactly the aligned page.
        stage4_input = CropInput(evidence.stage4.crop_id, evidence.page.png_bytes)
        if (
            hashlib.sha256(stage4_input.png_bytes).hexdigest()
            != evidence.stage4.source_sha256
        ):
            raise ValueError("Stage 4 candidate source digest disagrees with the page")
        if CropEnhancementArtifactWriter._source_size(stage4_input) != (
            evidence.stage4.width,
            evidence.stage4.height,
        ):
            raise ValueError("Stage 4 candidate changed the aligned page geometry")
        for block, crop in zip(evidence.plan.blocks, evidence.crops):
            if crop.gamma.recipe != evidence.stage4.recipe:
                raise ValueError(
                    f"Stage 5 gamma recipe disagrees with Stage 4 for {block.block_id}"
                )
            if crop.gamma.backend is not evidence.stage4.backend:
                raise ValueError(
                    f"Stage 5 gamma backend disagrees with Stage 4 for {block.block_id}"
                )
            if (crop.gamma.width, crop.gamma.height) != (
                block.bbox.width,
                block.bbox.height,
            ):
                raise ValueError(
                    f"Stage 5 gamma changed bbox geometry for {block.block_id}"
                )
        return stage4_input

    @staticmethod
    def _validate_control(control: RunOutcome[str]) -> None:
        if (
            control.status is not RunStatus.COMPLETE
            or control.root is None
            or control.error is not None
            or control.unresolved_node_ids
        ):
            raise ValueError("tutorial control must be a complete RunOutcome")
        if (
            not control.evidence
            or not control.derivations
            or not control.trace
            or control.steps < 1
        ):
            raise ValueError("tutorial control evidence must not be empty")

        stage_order: list[int] = []
        for atom in control.evidence:
            prefix, separator, _name = atom.payload.partition(":")
            if not separator or not prefix.isdecimal():
                raise ValueError("control evidence does not encode a stage number")
            stage_order.append(int(prefix))
        if tuple(stage_order) != PIPELINE_ORDER:
            raise ValueError("control evidence reordered the sparse pipeline stages")

        atom_ids = tuple(atom.atom_id for atom in control.evidence)
        if control.root.atom_ids != atom_ids:
            raise ValueError("control root does not preserve the stage evidence order")
        if tuple(atom.order_key for atom in control.evidence) != tuple(
            sorted(atom.order_key for atom in control.evidence)
        ):
            raise ValueError("control evidence order keys are not monotonic")
        if len(atom_ids) != len(set(atom_ids)):
            raise ValueError("control evidence identifiers are not unique")
        if control.derivations[-1] != control.root:
            raise ValueError("control derivations are not in completed post-order")
        trace_steps = tuple(item.step for item in control.trace)
        if trace_steps != tuple(sorted(trace_steps)):
            raise ValueError("control trace steps are not monotonic")

    @staticmethod
    def _validate_geometry(
        geometry: GeometryBundle,
        *,
        evidence: SparsePipelineEvidence,
    ) -> None:
        if geometry.result != evidence.geometry:
            raise ValueError("geometry bundle and sparse pipeline evidence disagree")
        original_width, original_height = (
            geometry.result.alignment.transform.original_size
        )
        aligned_width, aligned_height = geometry.result.segmentation.aligned_size
        expected_shapes = (
            ("source_rgb", geometry.source_rgb, (original_height, original_width, 3)),
            ("aligned_rgb", geometry.aligned_rgb, (aligned_height, aligned_width, 3)),
            ("foreground_mask", geometry.foreground_mask, (aligned_height, aligned_width)),
            ("rule_mask", geometry.rule_mask, (aligned_height, aligned_width)),
            ("ownership", geometry.ownership, (aligned_height, aligned_width)),
        )
        for name, value, expected_shape in expected_shapes:
            if not isinstance(value, np.ndarray) or value.shape != expected_shape:
                raise ValueError(f"geometry {name} has the wrong array shape")
        if geometry.source_rgb.dtype != np.uint8 or geometry.aligned_rgb.dtype != np.uint8:
            raise ValueError("geometry RGB arrays must contain uint8 values")
        if geometry.foreground_mask.dtype != np.bool_ or geometry.rule_mask.dtype != np.bool_:
            raise ValueError("geometry masks must contain boolean values")
        if not np.issubdtype(geometry.ownership.dtype, np.integer):
            raise ValueError("geometry ownership must contain integer values")
        if evidence.ownership is not None and not np.array_equal(
            geometry.ownership,
            evidence.ownership,
        ):
            raise ValueError(
                "geometry bundle and sparse evidence ownership disagree"
            )
        expected_ownership_order = tuple(
            segment.segment_id
            for segment in geometry.result.segmentation.segments
        )
        if (
            evidence.ownership_segment_ids is not None
            and evidence.ownership_segment_ids != expected_ownership_order
        ):
            raise ValueError(
                "geometry bundle and sparse evidence ownership order disagree"
            )
        aligned_sha256 = hashlib.sha256(
            memoryview(np.ascontiguousarray(geometry.aligned_rgb))
        ).hexdigest()
        if aligned_sha256 != geometry.result.aligned_rgb_sha256:
            raise ValueError("geometry aligned RGB digest disagrees with its result")

    @staticmethod
    def _write_control(stage: Path, control: RunOutcome[str]) -> None:
        stage.mkdir()
        helper = ControlArtifactWriter
        helper._write_json(
            stage / "manifest.json",
            {
                "semantic_stage": 3,
                "execution_step": 1,
                "stage_name": "recursive-control",
                "status": "COMPLETE",
                "source_status": control.status.value,
                "steps": control.steps,
                "derivations": len(control.derivations),
                "evidence_atoms": len(control.evidence),
                "trace_events": len(control.trace),
                "root_result_id": control.root.result_id,
                "execution_order": list(PIPELINE_ORDER),
                "unresolved_node_ids": [],
                "error": None,
            },
        )
        helper._write_jsonl(
            stage / "evidence.jsonl",
            (asdict(item) for item in control.evidence),
        )
        helper._write_jsonl(
            stage / "trace.jsonl",
            (asdict(item) for item in control.trace),
        )
        helper._write_jsonl(
            stage / "derivations.jsonl",
            (asdict(item) for item in control.derivations),
        )
        helper._write_text(
            stage / "evidence.txt",
            "".join(
                f"{item.order_key!r}\t{item.atom_id}\t{item.payload}\n"
                for item in control.evidence
            ),
        )
        helper._write_text(
            stage / "trace.txt",
            "".join(
                (
                    f"{item.step:06d} {item.event:<8} {item.node_id} "
                    + " ".join(f"{key}={value}" for key, value in item.details)
                ).rstrip()
                + "\n"
                for item in control.trace
            ),
        )
        helper._write_text(
            stage / "result.txt",
            "\n".join(
                (
                    f"result_id={control.root.result_id}",
                    f"node_id={control.root.node_id}",
                    f"kind={control.root.kind}",
                    "atom_ids=" + ",".join(control.root.atom_ids),
                    "",
                )
            ),
        )

    @staticmethod
    def _write_enhancement_visuals(
        stage: Path,
        *,
        source: CropInput,
        result: EnhancedCrop,
    ) -> None:
        with Image.open(io.BytesIO(source.png_bytes)) as opened_source:
            source_rgb = opened_source.convert("RGB")
        with Image.open(io.BytesIO(result.png_bytes)) as opened_result:
            result_rgb = opened_result.convert("RGB")
        try:
            difference = ImageChops.difference(source_rgb, result_rgb)
            try:
                difference.save(stage / "abs-diff.png", format="PNG")
            finally:
                difference.close()
        finally:
            source_rgb.close()
            result_rgb.close()
        TutorialArtifactWriter._write_json(
            stage / "tutorial-visuals.json",
            {
                "candidate_gallery": "candidate-gallery.md",
                "full_page_source": f"source/{source.crop_id}.png",
                "full_page_output": f"output/{result.crop_id}.png",
                "abs_diff": "abs-diff.png",
                "definition": "per-channel absolute difference: abs(source-enhanced)",
                "black_pixel_meaning": "no channel changed",
            },
        )
        (stage / "candidate-gallery.md").write_text(
            "\n".join(
                (
                    "# Actual Stage 4 full-page candidate",
                    "",
                    "**GLOBAL CALIBRATION ONLY — NOT OCR INPUT.** Этот optional "
                    "full-page candidate нужен только для сверки recipe/backend. "
                    "Production enhancement применяется отдельно к каждому raw block.",
                    "",
                    f"size=`{result.width}×{result.height}`; recipe=`{result.recipe}`; "
                    f"backend=`{result.backend.value}`; role=`optional candidate`.",
                    "",
                    "| actual full-page source | actual full-page enhanced output |",
                    "|---|---|",
                    f"| ![full-page source](source/{source.crop_id}.png) | "
                    f"![full-page enhanced](output/{result.crop_id}.png) |",
                    "",
                )
            ),
                encoding="utf-8",
            )

    @classmethod
    def _write_block_isolation_evidence(
        cls,
        target: Path,
        *,
        crop: BlockCropPair,
        block: RecognitionBlock,
        object_id: str,
        owner_by_segment: Mapping[str, str],
        ownership_sha256: str,
        ownership_order: tuple[str, ...],
    ) -> str | None:
        """Persist the exact object-local mask and its Stage 1 provenance."""

        isolation_mask_name: str | None = None
        isolation_mask_sha256: str | None = None
        if crop.isolation_mask_png is not None:
            isolation_mask_name = "isolation-mask.png"
            (target / isolation_mask_name).write_bytes(crop.isolation_mask_png)
            isolation_mask_sha256 = hashlib.sha256(
                crop.isolation_mask_png
            ).hexdigest()
        masked_owners = {
            segment_id: owner_by_segment[segment_id]
            for segment_id in crop.masked_segment_ids
        }
        cls._write_json(
            target / "isolation.json",
            {
                "applied": crop.isolation_mask_png is not None,
                "mask_png": isolation_mask_name,
                "mask_sha256": isolation_mask_sha256,
                "mask_semantics": (
                    "white pixels were removed from the OCR input; "
                    "black pixels were preserved"
                ),
                "masked_segment_ids": list(crop.masked_segment_ids),
                "masked_segment_owners": masked_owners,
                "view_object_id": object_id,
                "block_member_segment_ids": list(block.segment_ids),
                "provenance": {
                    "source_stage": 1,
                    "source": "exact Stage 1 ownership raster",
                    "ownership_raster_sha256": ownership_sha256,
                    "ownership_segment_ids": list(ownership_order),
                    "validation": (
                        "mask and masked IDs were recomputed from Stage 1 "
                        "ownership and checked before artifact publication"
                    ),
                    "declared_members_preserved": True,
                    "masked_segments_are_foreign_objects": all(
                        owner != object_id for owner in masked_owners.values()
                    ),
                },
            },
        )
        return isolation_mask_name

    @classmethod
    def _write_block_crop_gallery(
        cls,
        stage: Path,
        *,
        evidence: SparsePipelineEvidence,
    ) -> None:
        assert evidence.stage4 is not None
        entries: list[dict[str, object]] = []
        contact_items: list[tuple[str, Path, Path]] = []
        for block, crop in zip(evidence.plan.blocks, evidence.crops):
            raw_path = stage / "raw" / f"{block.block_id}.png"
            gamma_path = stage / "gamma" / f"{block.block_id}.png"
            contact_items.append((block.block_id, raw_path, gamma_path))
            entries.append(
                {
                    "block_id": block.block_id,
                    "bbox": list(block.bbox.as_tuple()),
                    "width": block.bbox.width,
                    "height": block.bbox.height,
                    "core_segment_ids": list(block.core_segment_ids),
                    "context_segment_ids": list(block.context_segment_ids),
                    "segment_ids": list(block.segment_ids),
                    "object_ids": list(block.object_ids),
                    "raw": raw_path.relative_to(stage).as_posix(),
                    "gamma": gamma_path.relative_to(stage).as_posix(),
                    "isolation_mask": (
                        f"isolation-masks/{block.block_id}.png"
                        if crop.isolation_mask_png is not None
                        else None
                    ),
                    "masked_segment_ids": list(crop.masked_segment_ids),
                    "raw_sha256": hashlib.sha256(crop.raw.png_bytes).hexdigest(),
                    "gamma_sha256": crop.gamma.output_sha256,
                    "gamma_recipe": crop.gamma.recipe,
                    "gamma_backend": crop.gamma.backend.value,
                    "gamma_recipe_matches_stage4": (
                        crop.gamma.recipe == evidence.stage4.recipe
                    ),
                    "gamma_backend_matches_stage4": (
                        crop.gamma.backend is evidence.stage4.backend
                    ),
                    "gamma_geometry_unchanged": (
                        (crop.gamma.width, crop.gamma.height)
                        == (block.bbox.width, block.bbox.height)
                    ),
                }
            )
        recipe_matches = all(
            item["gamma_recipe_matches_stage4"] is True for item in entries
        )
        backend_matches = all(
            item["gamma_backend_matches_stage4"] is True for item in entries
        )
        geometry_unchanged = all(
            item["gamma_geometry_unchanged"] is True for item in entries
        )
        contact_sheets = write_paired_contact_sheets(
            stage,
            stem="blocks",
            first_label="raw",
            second_label="gamma",
            items=tuple(contact_items),
        )
        cls._write_json(
            stage / "crop-gallery.json",
            {
                "schema": "sparse-block-crops-v1",
                "enhancement_owner_stage": 4,
                "membership_and_algebra_owner_stage": 5,
                "definition": {
                    "raw": "exact Stage 5 bbox crop supplied to the raw OCR job",
                    "gamma": "exact enhanced candidate supplied to the gamma OCR job",
                },
                "blocks": len(entries),
                "contact_sheets": contact_sheets,
                "stage4_calibration": {
                    "manifest": "../04-enhancement/manifest.json",
                    "scope": "full aligned page",
                    "recipe": evidence.stage4.recipe,
                    "backend": evidence.stage4.backend.value,
                },
                "stage5_application": {
                    "scope": "each raw block independently",
                    "relation_to_stage4_output": (
                        "same recipe/backend; recomputed from each raw block, "
                        "not cropped from the full-page enhanced output"
                    ),
                },
                "invariants": {
                    "gamma_recipe_matches_stage4": recipe_matches,
                    "gamma_backend_matches_stage4": backend_matches,
                    "gamma_geometry_unchanged": geometry_unchanged,
                    "gamma_recomputed_per_raw_block": True,
                },
                "items": entries,
            },
        )
        cls._write_block_gallery(
            stage / "crop-gallery.md",
            entries=tuple(entries),
            contact_sheets=contact_sheets,
        )
        cls._write_production_enhancement_evidence(
            stage.parent / "04-enhancement",
            entries=tuple(entries),
            evidence=evidence,
        )
        cls._write_membership_gallery(
            stage / "membership-gallery.md",
            entries=tuple(entries),
            evidence=evidence,
        )

    @classmethod
    def _write_production_enhancement_evidence(
        cls,
        stage: Path,
        *,
        entries: tuple[dict[str, object], ...],
        evidence: SparsePipelineEvidence,
    ) -> None:
        assert evidence.stage4 is not None
        items = [
            {
                "block_id": item["block_id"],
                "bbox": item["bbox"],
                "width": item["width"],
                "height": item["height"],
                "source": f"../05-blocks/{item['raw']}",
                "output": f"../05-blocks/{item['gamma']}",
                "source_sha256": item["raw_sha256"],
                "output_sha256": item["gamma_sha256"],
                "recipe": item["gamma_recipe"],
                "backend": item["gamma_backend"],
                "same_geometry": item["gamma_geometry_unchanged"],
                "candidate_role": "actual per-block OCR candidate",
            }
            for item in entries
        ]
        cls._write_json(
            stage / "production-block-manifest.json",
            {
                "semantic_stage": 4,
                "stage_name": "block-local-enhancement",
                "scope": "each Stage 5 raw block independently",
                "status": "complete",
                "blocks": len(items),
                "recipe": evidence.stage4.recipe,
                "backend": evidence.stage4.backend.value,
                "selection_deferred_to_stage2": True,
                "global_calibration": {
                    "manifest": "manifest.json",
                    "gallery": "candidate-gallery.md",
                    "scope": "full aligned page",
                    "not_ocr_input": True,
                },
                "invariants": {
                    "per_block_source_is_stage5_raw_crop": True,
                    "per_block_output_is_recomputed_from_source": True,
                    "same_geometry": all(
                        item["same_geometry"] is True for item in items
                    ),
                    "recipe_backend_match_global_calibration": True,
                    "full_page_output_not_used_as_ocr_input": True,
                },
                "items": items,
            },
        )
        lines = [
            "# Production Stage 4: actual block-local enhancement",
            "",
            "Основной production flow: каждый Stage 5 raw block улучшается "
            "независимо из-за различающегося фона. Обе actual raw/gamma версии "
            "передаются Stage 2 как OCR candidates.",
            "",
            "Full-page output — только global calibration / **NOT OCR INPUT**; "
            "block gamma не вырезается из него.",
            "",
        ]
        for item in items:
            block_id = str(item["block_id"])
            lines.extend(
                (
                    f"## `{block_id}`",
                    "",
                    f"bbox=`{item['bbox']}`; size=`{item['width']}×{item['height']}`; "
                    f"recipe/backend=`{item['recipe']}/{item['backend']}`; "
                    f"same geometry=`{item['same_geometry']}`.",
                    "",
                    "| actual raw block | actual enhanced block |",
                    "|---|---|",
                    f"| ![{block_id} raw]({item['source']}) | "
                    f"![{block_id} gamma]({item['output']}) |",
                    "",
                )
            )
        (stage / "production-block-gallery.md").write_text(
            "\n".join(lines), encoding="utf-8"
        )

    @staticmethod
    def _write_membership_gallery(
        path: Path,
        *,
        entries: tuple[dict[str, object], ...],
        evidence: SparsePipelineEvidence,
    ) -> None:
        lines = [
            "# Stage 5: actual blocks, memberships and OR/XOR",
            "",
            "Stage 5 отвечает за bbox, core/context membership и точную "
            "алгебру соседних блоков. Enhancement raw→gamma документируется в Stage 4.",
            "",
        ]
        for item in entries:
            block_id = str(item["block_id"])
            lines.extend(
                (
                    f"## `{block_id}`",
                    "",
                    f"bbox=`{item['bbox']}`; size=`{item['width']}×{item['height']}`; "
                    f"core=`{item['core_segment_ids']}`; "
                    f"context=`{item['context_segment_ids']}`; "
                    f"members=`{item['segment_ids']}`.",
                    "",
                    f"![{block_id} actual raw block]({item['raw']})",
                    "",
                )
            )
        if evidence.plan.adjacent_algebra:
            lines.extend(("## OR/XOR соседних блоков", ""))
        for index, algebra in enumerate(evidence.plan.adjacent_algebra):
            pair_root = f"adjacent-pairs/pair-{index:06d}"
            lines.extend(
                (
                    f"### `{algebra.first_block_id}` + `{algebra.second_block_id}`",
                    "",
                    f"intersection=`{list(algebra.intersection_segment_ids)}`; "
                    f"OR/union=`{list(algebra.union_segment_ids)}`; "
                    f"XOR=`{list(algebra.xor_segment_ids)}`. "
                    f"[manifest]({pair_root}/manifest.json)",
                    "",
                )
            )
            if index < TutorialArtifactWriter._MAX_RENDERED_PAIR_VISUALS:
                lines.extend(
                    (
                        "| intersection ink | OR/union ink | XOR ink |",
                        "|---|---|---|",
                        f"| ![intersection]({pair_root}/intersection-ink-mask.png) | "
                        f"![union]({pair_root}/union-ink-mask.png) | "
                        f"![xor]({pair_root}/xor-ink-mask.png) |",
                        "",
                    )
                )
        path.write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _write_block_gallery(
        path: Path,
        *,
        entries: tuple[dict[str, object], ...],
        contact_sheets: list[str],
    ) -> None:
        lines = [
            "# Actual Stage 5 block crops",
            "",
            "Каждая пара ниже — реальные PNG, переданные двум OCR jobs, без "
            "дорисованной обводки на исходной странице.",
            "Production Stage 4 пересчитывает gamma независимо из каждого raw "
            "block. Full-page candidate — только global calibration / NOT OCR "
            "INPUT; block gamma не вырезан из него.",
            "",
        ]
        if contact_sheets:
            lines.extend(("## Контактные листы", ""))
            lines.extend(f"![{item}]({item})" for item in contact_sheets)
        for entry in entries:
            block_id = str(entry["block_id"])
            lines.extend(
                (
                    "",
                    f"## `{block_id}`",
                    "",
                    f"bbox=`{entry['bbox']}`; size=`{entry['width']}×{entry['height']}`; "
                    f"core=`{entry['core_segment_ids']}`; "
                    f"context=`{entry['context_segment_ids']}`; "
                    f"members=`{entry['segment_ids']}`; "
                    f"masked foreign=`{entry['masked_segment_ids']}`; "
                    f"gamma=`{entry['gamma_recipe']}/{entry['gamma_backend']}`; "
                    f"same geometry=`{entry['gamma_geometry_unchanged']}`.",
                    "",
                    "| raw OCR input | gamma OCR input |",
                    "|---|---|",
                    f"| ![{block_id} raw]({entry['raw']}) | "
                    f"![{block_id} gamma]({entry['gamma']}) |",
                )
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @classmethod
    def _write_block_visuals(
        cls,
        stage: Path,
        *,
        geometry: GeometryBundle,
        evidence: SparsePipelineEvidence,
    ) -> None:
        overlay = cls._page_rgb(evidence.page.png_bytes)
        try:
            draw = ImageDraw.Draw(overlay)
            palette = (
                (20, 105, 225),
                (235, 120, 20),
                (120, 55, 200),
                (15, 160, 120),
            )
            for index, block in enumerate(evidence.plan.blocks):
                color = palette[index % len(palette)]
                box = block.bbox
                draw.rectangle(
                    (box.left, box.top, box.right - 1, box.bottom - 1),
                    outline=color,
                    width=2,
                )
                draw.text((box.left + 3, box.top + 2), block.block_id, fill=color)
            overlay.save(stage / "page-block-overlay.png", format="PNG")
        finally:
            overlay.close()

        segment_index = {
            item.segment_id: index
            for index, item in enumerate(evidence.geometry.segmentation.segments)
        }
        pair_entries: list[dict[str, object]] = []
        pairs_root = stage / "adjacent-pairs"
        pairs_root.mkdir()
        for pair_index, algebra in enumerate(evidence.plan.adjacent_algebra):
            pair_dir = pairs_root / f"pair-{pair_index:06d}"
            pair_dir.mkdir()
            sets = (
                ("intersection", algebra.intersection_segment_ids),
                ("union", algebra.union_segment_ids),
                ("xor", algebra.xor_segment_ids),
            )
            files: dict[str, dict[str, str]] = {}
            render_visuals = pair_index < cls._MAX_RENDERED_PAIR_VISUALS
            if render_visuals:
                for operation, segment_ids in sets:
                    ink_mask = cls._ink_mask(
                        geometry,
                        segment_ids=segment_ids,
                        segment_index=segment_index,
                    )
                    bbox_mask = cls._bbox_mask(
                        evidence,
                        segment_ids=segment_ids,
                    )
                    ink_path = pair_dir / f"{operation}-ink-mask.png"
                    bbox_path = pair_dir / f"{operation}-bbox-mask.png"
                    cls._save_mask(ink_path, ink_mask)
                    cls._save_mask(bbox_path, bbox_mask)
                    files[operation] = {
                        "ink_region": ink_path.relative_to(stage).as_posix(),
                        "bbox": bbox_path.relative_to(stage).as_posix(),
                    }
            entry = {
                "first_block_id": algebra.first_block_id,
                "second_block_id": algebra.second_block_id,
                "intersection_segment_ids": list(algebra.intersection_segment_ids),
                "union_segment_ids": list(algebra.union_segment_ids),
                "xor_segment_ids": list(algebra.xor_segment_ids),
                "mask_basis": {
                    "ink_region": "stage-1 ownership intersected with foreground-mask",
                    "bbox": "filled half-open stage-1 segment bounding boxes",
                },
                "files": files,
                "visuals_rendered": render_visuals,
            }
            cls._write_json(pair_dir / "manifest.json", entry)
            pair_entries.append(entry)
        cls._write_json(
            stage / "tutorial-visuals.json",
            {
                "page_block_overlay": "page-block-overlay.png",
                "adjacent_pair_count": len(pair_entries),
                "rendered_adjacent_pair_count": min(
                    len(pair_entries),
                    cls._MAX_RENDERED_PAIR_VISUALS,
                ),
                "visual_pair_limit": cls._MAX_RENDERED_PAIR_VISUALS,
                "adjacent_pairs": pair_entries,
                "mask_semantics": "white=set member geometry; black=outside set",
            },
        )

    @staticmethod
    def _ink_mask(
        geometry: GeometryBundle,
        *,
        segment_ids: tuple[str, ...],
        segment_index: dict[str, int],
    ) -> np.ndarray:
        output = np.zeros(geometry.foreground_mask.shape, dtype=np.bool_)
        for segment_id in segment_ids:
            output |= geometry.ownership == segment_index[segment_id]
        return output & geometry.foreground_mask

    @staticmethod
    def _bbox_mask(
        evidence: SparsePipelineEvidence,
        *,
        segment_ids: tuple[str, ...],
    ) -> np.ndarray:
        width, height = evidence.geometry.segmentation.aligned_size
        output = np.zeros((height, width), dtype=np.bool_)
        segment_by_id = {
            item.segment_id: item
            for item in evidence.geometry.segmentation.segments
        }
        for segment_id in segment_ids:
            box = segment_by_id[segment_id].bbox
            output[box.top : box.bottom, box.left : box.right] = True
        return output

    @classmethod
    def _write_ocr_visuals(
        cls,
        stage: Path,
        *,
        evidence: SparsePipelineEvidence,
    ) -> None:
        image = cls._page_rgb(evidence.page.png_bytes)
        assigned_count = 0
        unassigned_count = len(evidence.fusion.unassigned_word_observations)
        try:
            draw = ImageDraw.Draw(image)
            for observation in evidence.fusion.observations:
                for box in observation.page_bboxes:
                    assigned_count += 1
                    draw.rectangle(
                        (box.left, box.top, box.right - 1, box.bottom - 1),
                        outline=(0, 170, 70),
                        width=2,
                    )
            for observation in evidence.fusion.unassigned_word_observations:
                box = observation.page_bbox
                draw.rectangle(
                    (box.left, box.top, box.right - 1, box.bottom - 1),
                    outline=(220, 35, 45),
                    width=2,
                )
            image.save(stage / "word-box-overlay.png", format="PNG")
        finally:
            image.close()
        cls._write_json(
            stage / "tutorial-visuals.json",
            {
                "word_box_overlay": "word-box-overlay.png",
                "assigned": {
                    "color_rgb": [0, 170, 70],
                    "box_count": assigned_count,
                    "source": "routed fusion observation page_bboxes",
                },
                "unassigned": {
                    "color_rgb": [220, 35, 45],
                    "box_count": unassigned_count,
                    "source": "fail-closed unassigned word page_bbox",
                },
            },
        )

    @staticmethod
    def _page_rgb(png_bytes: bytes) -> Image.Image:
        with Image.open(io.BytesIO(png_bytes)) as opened:
            opened.load()
            if opened.mode in {"RGBA", "LA"} or "transparency" in opened.info:
                rgba = opened.convert("RGBA")
                canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                try:
                    canvas.alpha_composite(rgba)
                    return canvas.convert("RGB")
                finally:
                    rgba.close()
                    canvas.close()
            return opened.convert("RGB")

    @staticmethod
    def _save_mask(path: Path, mask: np.ndarray) -> None:
        image = Image.fromarray(mask.astype(np.uint8) * 255, mode="L")
        try:
            image.save(path, format="PNG")
        finally:
            image.close()

    @staticmethod
    def _stage_statuses(
        *,
        evidence: SparsePipelineEvidence,
        document: DocumentAssemblyResult,
    ) -> tuple[str, ...]:
        geometry_status = (
            "COMPLETE"
            if evidence.geometry.status.value == "complete"
            else "UNRESOLVED"
        )
        block_status = (
            "COMPLETE"
            if evidence.plan.status.value == "complete"
            else "UNRESOLVED"
        )
        ocr_complete = (
            evidence.fusion.status is OcrFusionStatus.COMPLETE
            and evidence.queue.status is OcrQueueStatus.COMPLETE
            and not evidence.fusion.unassigned_word_observations
            and not evidence.fusion.replica_conflicts
        )
        document_status = (
            "COMPLETE"
            if document.status is AssemblyStatus.COMPLETE
            else "UNRESOLVED"
        )
        return (
            "COMPLETE",
            geometry_status,
            "COMPLETE",
            "COMPLETE",
            block_status,
            "COMPLETE" if ocr_complete else "UNRESOLVED",
            document_status,
        )

    @classmethod
    def _write_index(
        cls,
        root: Path,
        *,
        run_id: str,
        statuses: tuple[str, ...],
        evidence: SparsePipelineEvidence,
    ) -> None:
        if len(statuses) != len(PIPELINE_ORDER):
            raise ValueError("tutorial status count disagrees with execution order")
        if any(item not in _STATUS_VOCABULARY for item in statuses):
            raise ValueError("tutorial contains an unknown stage status")
        stages = []
        for step, (number, name, directory, status) in enumerate(
            zip(PIPELINE_ORDER, _STAGE_NAMES, _STAGE_DIRECTORIES, statuses),
            start=1,
        ):
            stages.append(
                {
                    "execution_step": step,
                    "semantic_stage": number,
                    "stage_name": name,
                    "status": status,
                    "directory": directory,
                    "manifest": f"{directory}/manifest.json",
                }
            )
        overall = "COMPLETE" if all(item == "COMPLETE" for item in statuses) else "UNRESOLVED"
        cls._write_json(
            root / "manifest.json",
            {
                "schema": "sparse-pipeline-tutorial-v1",
                "run_id": run_id,
                "status": overall,
                "status_vocabulary": list(_STATUS_VOCABULARY),
                "execution_order": list(PIPELINE_ORDER),
                "atomic_no_replace": True,
                "tutorial": "tutorial.md",
                "provenance": "provenance.json",
                "sparse_matrix": {
                    "exact_json": "sparse-matrix.json",
                    "exact_tsv": "sparse-matrix.tsv",
                    "logical_image": "sparse-matrix.png",
                    "literal_ownership_image": "sparse-matrix-ownership.png",
                },
                "object_debug": "objects/index.md",
                "stage_logs": "logs/stages.tsv",
                "final_document": {
                    "markdown": "document.md",
                    "text": "document.txt",
                },
                "stages": stages,
                "invariants": {
                    "control_complete_and_ordered": True,
                    "geometry_bundle_bound": True,
                    "segment_crops_exact": True,
                    "stage4_global_calibration_present": True,
                    "stage4_global_calibration_not_ocr_input": True,
                    "stage4_block_local_production_present": True,
                    "block_crops_exact": True,
                    "stage4_stage5_recipe_backend_match": True,
                    "stage5_gamma_geometry_unchanged": True,
                    "document_rederived": True,
                    "object_debug_uses_literal_stage_crops": True,
                    "object_debug_does_not_use_overlays_as_evidence": True,
                    "relative_links_only": True,
                },
            },
        )

        pair_link = ""
        if evidence.plan.adjacent_algebra:
            pair_link = (
                "; [первая OR/XOR-пара]"
                "(05-blocks/adjacent-pairs/pair-000000/manifest.json)"
            )
        rows = (
            (3, "03-control", statuses[0], "trace.txt", "порядок и while-трасса"),
            (
                1,
                "01-geometry",
                statuses[1],
                "segment-crops/gallery.md",
                "все реальные raw/isolated сегменты",
            ),
            (6, "06-objects", statuses[2], "reading-order.txt", "объекты и порядок чтения"),
            (
                4,
                "04-enhancement",
                statuses[3],
                "production-block-gallery.md",
                "actual per-block raw/enhanced production candidates",
            ),
            (
                5,
                "05-blocks",
                statuses[4],
                "membership-gallery.md",
                "actual blocks, memberships и OR/XOR",
            ),
            (2, "02-ocr", statuses[5], "word-box-overlay.png", "assigned/unassigned OCR boxes"),
            (7, "07-document", statuses[6], "candidate/document.md", "собранный документ"),
        )
        lines = [
            f"# Учебный sparse-прогон `{run_id}`",
            "",
            f"Итог: **{overall}**. Этапы исполняются строго как `3 → 1 → 6 → 4 → 5 → 2 → 7`.",
            "",
            "| Шаг | Этап | Статус | Что открыть |",
            "|---:|---:|---|---|",
        ]
        for step, (number, directory, status, artifact, description) in enumerate(rows, start=1):
            suffix = pair_link if number == 5 else ""
            lines.append(
                f"| {step} | {number} | {status} | "
                f"[{description}]({directory}/{artifact}){suffix} |"
            )
        lines.extend(
            (
                "",
                "## Быстрый ручной разбор",
                "",
                "- [точная разряженная матрица](sparse-matrix.tsv) и "
                "[её логическая картинка](sparse-matrix.png);",
                "- [literal Stage 1 ownership](sparse-matrix-ownership.png);",
                "- [объекты с собственными segments/ и blocks/](objects/index.md);",
                "- [логи этапов](logs/stages.tsv);",
                "- [commit, argv, config и input SHA](provenance.json);",
                "- [итоговый document.md](document.md) и "
                "[document.txt](document.txt).",
                "",
                "## Как читать визуализации",
                "",
                "- Stage 1: `segment-crops/gallery.md` показывает отдельный "
                "raw bbox crop и isolated ownership crop каждого сегмента. "
                "Это фактические пиксели результата, а не рамки на странице.",
                "- Stage 4: `production-block-gallery.md` показывает actual "
                "raw→gamma для каждого блока. Улучшение пересчитывается локально "
                "из-за разного фона. Full-page gallery — только global "
                "calibration / NOT OCR INPUT.",
                "- Stage 5: `membership-gallery.md` показывает actual block bbox, "
                "core/context membership и для каждой соседней пары OR/union, "
                "XOR и intersection как точные ink-region маски и как явно "
                "подписанные bbox-маски.",
                "- Stage 2: зелёные рамки — привязанные OCR-наблюдения, "
                "красные — слова, которые маршрутизатор оставил unassigned "
                "(fail-closed).",
                "- `UNRESOLVED` — артефакт сохранён для разбора, но не "
                "сертифицирован. `FAILED` означает, что этап не дал "
                "допустимого результата; при ошибке до публикации вся "
                "транзакция удаляется.",
                "",
                "[Машиночитаемый manifest](manifest.json)",
                "",
            )
        )
        (root / "tutorial.md").write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )


__all__ = ["TutorialArtifactWriter"]
