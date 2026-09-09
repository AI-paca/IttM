from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

from app.sparse_pipeline.block_crops import BlockCropPair
from app.sparse_pipeline.block_planning import (
    BlockPlan,
    BlockPlanningMode,
    sparse_matrix_payload,
    sparse_matrix_sha256,
)
from app.sparse_pipeline.contracts import Segment, SparseSegmentMatrix
from app.sparse_pipeline.ocr_fusion import (
    OcrEvidenceFusion,
    OcrFusionConfig,
    OcrFusionResult,
)
from app.sparse_pipeline.ocr_queue import OcrJobStatus, OcrQueueResult

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class OcrArtifactWriter:
    """Publish one self-contained Stage 2 evidence tree without overwriting."""

    def write(
        self,
        root: Path,
        *,
        run_id: str,
        plan: BlockPlan,
        segments: tuple[Segment, ...],
        crops: tuple[BlockCropPair, ...],
        queue: OcrQueueResult,
        fusion: OcrFusionResult,
        fusion_config: OcrFusionConfig | None = None,
        matrix: SparseSegmentMatrix | None = None,
    ) -> Path:
        if not isinstance(root, Path):
            raise TypeError("root must be a Path")
        if type(run_id) is not str or not _SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError("run_id contains unsafe characters")
        if not isinstance(plan, BlockPlan):
            raise ValueError("plan must be a BlockPlan")
        if type(segments) is not tuple or any(not isinstance(item, Segment) for item in segments):
            raise ValueError("segments must be an immutable Segment tuple")
        if type(crops) is not tuple or any(not isinstance(item, BlockCropPair) for item in crops):
            raise ValueError("crops must be an immutable BlockCropPair tuple")
        if not isinstance(queue, OcrQueueResult):
            raise ValueError("queue must be an OcrQueueResult")
        if not isinstance(fusion, OcrFusionResult):
            raise ValueError("fusion must be an OcrFusionResult")
        if tuple(item.block_id for item in crops) != tuple(item.block_id for item in plan.blocks):
            raise ValueError("OCR crop order disagrees with the block plan")
        if fusion.source_segment_ids != plan.source_segment_ids:
            raise ValueError("OCR fusion and block plan segment order disagree")
        self._validate_matrix(plan=plan, matrix=matrix)

        # Artifact files are the durable trust boundary used by the following
        # stage.  Frozen dataclasses can still be constructed manually, so do
        # not publish a caller-provided fusion merely because its field types
        # look valid.  Re-run the deterministic, reference-free fusion before
        # creating even the output root.  This binds every job and observation
        # to the exact block, transform, crop bytes and source geometry, and it
        # rejects a forged digest, bbox, confidence or selection fail-closed.
        expected_fusion = OcrEvidenceFusion(fusion_config).fuse(
            plan=plan,
            segments=segments,
            crops=crops,
            queue=queue,
        )
        if fusion != expected_fusion:
            raise ValueError("OCR fusion evidence was not derived from the supplied " "segments, queue and crop bytes")

        root.mkdir(parents=True, exist_ok=True)
        destination = root / run_id
        if destination.exists():
            raise FileExistsError(f"debug run already exists: {destination}")
        temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=root))
        try:
            stage = temporary / "02-ocr"
            self._write_stage(
                stage,
                plan=plan,
                segments=segments,
                crops=crops,
                queue=queue,
                fusion=fusion,
                matrix=matrix,
            )
            temporary.rename(destination)
            return destination
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
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
            raise ValueError("spatial OCR artifacts require the Stage 1 matrix")
        if (
            matrix.segment_ids() != frozenset(plan.source_segment_ids)
            or sparse_matrix_sha256(matrix) != plan.matrix_sha256
        ):
            raise ValueError("spatial OCR artifact matrix disagrees with its plan")

    @staticmethod
    def _write_stage(
        stage: Path,
        *,
        plan: BlockPlan,
        segments: tuple[Segment, ...],
        crops: tuple[BlockCropPair, ...],
        queue: OcrQueueResult,
        fusion: OcrFusionResult,
        matrix: SparseSegmentMatrix | None,
    ) -> None:
        raw_dir = stage / "inputs" / "raw"
        gamma_dir = stage / "inputs" / "gamma"
        job_text_dir = stage / "jobs" / "text"
        segment_text_dir = stage / "segments" / "selected"
        group_text_dir = stage / "groups" / "selected"
        block_text_dir = stage / "blocks" / "unattributable"
        for directory in (
            raw_dir,
            gamma_dir,
            job_text_dir,
            segment_text_dir,
            group_text_dir,
            block_text_dir,
        ):
            directory.mkdir(parents=True)

        segment_entries = [
            {
                "segment_id": segment.segment_id,
                "bbox": list(segment.bbox.as_tuple()),
                "source_bbox": list(segment.source_bbox.as_tuple()),
                "kind": segment.kind.value,
                "ink_pixels": segment.ink_pixels,
                "row_index": segment.row_index,
                "order_key": list(segment.order_key),
                "parent_path": list(segment.parent_path),
                "component_ids": list(segment.component_ids),
            }
            for segment in segments
        ]
        plan_entry = {
            "aligned_size": list(plan.aligned_size),
            "source_segment_ids": list(plan.source_segment_ids),
            "status": plan.status.value,
            "diagnostics": list(plan.diagnostics),
            "blocks": [
                {
                    "block_id": block.block_id,
                    "bbox": list(block.bbox.as_tuple()),
                    "core_segment_ids": list(block.core_segment_ids),
                    "segment_ids": list(block.segment_ids),
                    "context_segment_ids": list(block.context_segment_ids),
                    "object_ids": list(block.object_ids),
                    "scope_id": block.scope_id,
                }
                for block in plan.blocks
            ],
            "membership_units": [
                {
                    "unit_id": item.unit_id,
                    "kind": item.kind.value,
                    "segment_ids": list(item.segment_ids),
                    "block_ids": list(item.block_ids),
                    "scope_id": item.scope_id,
                }
                for item in plan.membership_units
            ],
            "matrix_sha256": plan.matrix_sha256,
            "adjacent_algebra": [
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
            ],
        }
        _write_jsonl(stage / "inputs" / "segments.jsonl", segment_entries)
        (stage / "inputs" / "plan.json").write_text(
            json.dumps(plan_entry, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        if matrix is not None and plan.mode is BlockPlanningMode.SPATIAL_2D:
            matrix_record = {
                "sha256": sparse_matrix_sha256(matrix),
                **sparse_matrix_payload(matrix),
            }
            (stage / "inputs" / "matrix.json").write_text(
                json.dumps(
                    matrix_record,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

        crop_entries: list[dict[str, object]] = []
        for crop in crops:
            raw_path = raw_dir / f"{crop.block_id}.png"
            gamma_path = gamma_dir / f"{crop.block_id}.png"
            raw_path.write_bytes(crop.raw.png_bytes)
            gamma_path.write_bytes(crop.gamma.png_bytes)
            crop_entries.append(
                {
                    "block_id": crop.block_id,
                    "bbox": list(crop.bbox.as_tuple()),
                    "segment_ids": list(crop.segment_ids),
                    "raw": raw_path.relative_to(stage).as_posix(),
                    "gamma": gamma_path.relative_to(stage).as_posix(),
                    "raw_sha256": hashlib.sha256(crop.raw.png_bytes).hexdigest(),
                    "gamma_sha256": hashlib.sha256(crop.gamma.png_bytes).hexdigest(),
                }
            )

        job_entries: list[dict[str, object]] = []
        for job in queue.jobs:
            output = job.output
            text_path: str | None = None
            words: list[dict[str, object]] = []
            geometry: str | None = None
            if output is not None:
                path = job_text_dir / f"{job.job_id}.txt"
                path.write_text(output.text + "\n", encoding="utf-8")
                text_path = path.relative_to(stage).as_posix()
                geometry = output.geometry.value
                words = [
                    {
                        "text": word.text,
                        "bbox": list(word.bbox.as_tuple()),
                        "confidence": float(word.confidence),
                    }
                    for word in output.words
                ]
            job_entries.append(
                {
                    "job_id": job.job_id,
                    "block_id": job.block_id,
                    "transform": job.transform.value,
                    "lane_id": job.lane_id,
                    "capability_id": job.capability_id,
                    "resource": job.resource.value,
                    "status": job.status.value,
                    "geometry": geometry,
                    "text_path": text_path,
                    "words": words,
                    "input_sha256": job.input_sha256,
                    "context_sha256": job.context_sha256,
                    "failure_code": (job.failure_code.value if job.failure_code is not None else None),
                    "error_type": job.error_type,
                    "error_message": job.error_message,
                    "elapsed_seconds": float(job.elapsed_seconds),
                }
            )

        segment_entries: list[dict[str, object]] = []
        for segment_index, segment in enumerate(fusion.segments):
            # Segment IDs are evidence, not trusted filesystem components.
            path = segment_text_dir / f"segment-{segment_index:08d}.txt"
            path.write_text(
                (segment.selected_text or "") + "\n",
                encoding="utf-8",
            )
            segment_entries.append(
                {
                    "segment_id": segment.segment_id,
                    "selected_text": path.relative_to(stage).as_posix(),
                    "selected_observation_id": segment.selected_observation_id,
                    "selected_transform": (
                        segment.selected_transform.value if segment.selected_transform is not None else None
                    ),
                    "selected_lane_id": segment.selected_lane_id,
                    "confidence": segment.confidence,
                    "stability": segment.stability,
                    "observation_count": segment.observation_count,
                    "independent_context_count": (segment.independent_context_count),
                    "uncertainty_reasons": list(segment.uncertainty_reasons),
                    "script_scores": [list(item) for item in segment.script_scores],
                    "unresolved": segment.unresolved,
                }
            )

        block_text_entries: list[dict[str, object]] = []
        for index, observation in enumerate(fusion.block_text_observations):
            path = block_text_dir / f"block-text-{index:08d}.txt"
            path.write_text(observation.text + "\n", encoding="utf-8")
            block_text_entries.append(
                {
                    "job_id": observation.job_id,
                    "block_id": observation.block_id,
                    "transform": observation.transform.value,
                    "lane_id": observation.lane_id,
                    "capability_id": observation.capability_id,
                    "text_path": path.relative_to(stage).as_posix(),
                    "attribution_status": observation.attribution_status.value,
                    "attribution_reason": observation.attribution_reason,
                    "source_replica_conflict": (observation.source_replica_conflict),
                }
            )

        observation_entries = [
            {
                "observation_id": item.observation_id,
                "job_id": item.job_id,
                "segment_id": item.segment_id,
                "block_id": item.block_id,
                "transform": item.transform.value,
                "lane_id": item.lane_id,
                "capability_id": item.capability_id,
                "text": item.text,
                "confidence": item.confidence,
                "page_bboxes": [list(box.as_tuple()) for box in item.page_bboxes],
                "input_sha256": item.input_sha256,
                "context_sha256": item.context_sha256,
                "source_replica_conflict": item.source_replica_conflict,
            }
            for item in fusion.observations
        ]
        group_entries: list[dict[str, object]] = []
        for index, group in enumerate(fusion.segment_groups):
            path = group_text_dir / f"group-{index:08d}.txt"
            path.write_text((group.selected_text or "") + "\n", encoding="utf-8")
            group_entries.append(
                {
                    "unit_id": group.unit_id,
                    "segment_ids": list(group.segment_ids),
                    "selected_text": path.relative_to(stage).as_posix(),
                    "selected_observation_id": group.selected_observation_id,
                    "selected_transform": (
                        group.selected_transform.value if group.selected_transform is not None else None
                    ),
                    "selected_lane_id": group.selected_lane_id,
                    "confidence": group.confidence,
                    "stability": group.stability,
                    "observation_count": group.observation_count,
                    "independent_context_count": group.independent_context_count,
                    "uncertainty_reasons": list(group.uncertainty_reasons),
                    "unresolved": group.unresolved,
                }
            )
        group_observation_entries = [
            {
                "observation_id": item.observation_id,
                "job_id": item.job_id,
                "unit_id": item.unit_id,
                "segment_ids": list(item.segment_ids),
                "block_id": item.block_id,
                "transform": item.transform.value,
                "lane_id": item.lane_id,
                "capability_id": item.capability_id,
                "text": item.text,
                "confidence": item.confidence,
                "page_bboxes": [list(box.as_tuple()) for box in item.page_bboxes],
                "input_sha256": item.input_sha256,
                "context_sha256": item.context_sha256,
                "source_replica_conflict": item.source_replica_conflict,
            }
            for item in fusion.group_observations
        ]
        unassigned_entries = [
            {
                "job_id": item.job_id,
                "block_id": item.block_id,
                "transform": item.transform.value,
                "lane_id": item.lane_id,
                "capability_id": item.capability_id,
                "word_index": item.word_index,
                "text": item.text,
                "confidence": item.confidence,
                "page_bbox": list(item.page_bbox.as_tuple()),
                "input_sha256": item.input_sha256,
                "context_sha256": item.context_sha256,
                "reason": item.reason,
            }
            for item in fusion.unassigned_word_observations
        ]
        overlap_entries = [
            {
                "first_block_id": item.first_block_id,
                "second_block_id": item.second_block_id,
                "intersection_segment_ids": list(item.intersection_segment_ids),
                "union_segment_ids": list(item.union_segment_ids),
                "xor_segment_ids": list(item.xor_segment_ids),
                "confirmed_intersection_segment_ids": list(item.confirmed_intersection_segment_ids),
                "cross_transform_confirmed_intersection_segment_ids": list(
                    item.cross_transform_confirmed_intersection_segment_ids
                ),
                "near_confirmed_intersection_segment_ids": list(item.near_confirmed_intersection_segment_ids),
                "deferred_intersection_segment_ids": list(item.deferred_intersection_segment_ids),
                "conflicting_intersection_segment_ids": list(item.conflicting_intersection_segment_ids),
                "missing_intersection_segment_ids": list(item.missing_intersection_segment_ids),
                "observed_union_segment_ids": list(item.observed_union_segment_ids),
                "observed_xor_segment_ids": list(item.observed_xor_segment_ids),
            }
            for item in fusion.overlaps
        ]
        replica_entries = [
            {
                "context_sha256": item.context_sha256,
                "capability_id": item.capability_id,
                "transform": item.transform.value,
                "job_ids": list(item.job_ids),
                "evidence_sha256": list(item.evidence_sha256),
            }
            for item in fusion.replica_conflicts
        ]

        _write_jsonl(stage / "inputs" / "crops.jsonl", crop_entries)
        _write_jsonl(stage / "jobs" / "jobs.jsonl", job_entries)
        _write_jsonl(
            stage / "segments" / "observations.jsonl",
            observation_entries,
        )
        _write_jsonl(stage / "segments" / "selected.jsonl", segment_entries)
        if group_entries or group_observation_entries:
            _write_jsonl(stage / "groups" / "selected.jsonl", group_entries)
            _write_jsonl(
                stage / "groups" / "observations.jsonl",
                group_observation_entries,
            )
        _write_jsonl(stage / "blocks" / "text.jsonl", block_text_entries)
        _write_jsonl(stage / "unassigned-words.jsonl", unassigned_entries)
        _write_jsonl(stage / "overlaps.jsonl", overlap_entries)
        _write_jsonl(stage / "replica-conflicts.jsonl", replica_entries)

        failed_jobs = sum(job.status is OcrJobStatus.FAILED for job in queue.jobs)
        unresolved_segments = sum(item.unresolved for item in fusion.segments)
        overlap_exact_confirmed = sum(len(item.confirmed_intersection_segment_ids) for item in fusion.overlaps)
        overlap_cross_transform_confirmed = sum(
            len(item.cross_transform_confirmed_intersection_segment_ids) for item in fusion.overlaps
        )
        overlap_near_confirmed = sum(len(item.near_confirmed_intersection_segment_ids) for item in fusion.overlaps)
        overlap_deferred = sum(len(item.deferred_intersection_segment_ids) for item in fusion.overlaps)
        overlap_conflicts = sum(len(item.conflicting_intersection_segment_ids) for item in fusion.overlaps)
        overlap_missing = sum(len(item.missing_intersection_segment_ids) for item in fusion.overlaps)
        manifest = {
            "semantic_stage": 2,
            "execution_step": 6,
            "stage_name": "ocr-evidence-fusion",
            "status": fusion.status.value,
            "queue_status": queue.status.value,
            "blocks": len(plan.blocks),
            "segments": len(plan.source_segment_ids),
            "jobs": len(queue.jobs),
            "complete_jobs": queue.complete,
            "failed_jobs": failed_jobs,
            "segment_observations": len(fusion.observations),
            "segment_group_observations": len(fusion.group_observations),
            "segment_groups": len(fusion.segment_groups),
            "block_text_observations": len(fusion.block_text_observations),
            "unassigned_words": len(fusion.unassigned_word_observations),
            "replica_conflicts": len(fusion.replica_conflicts),
            "unresolved_segments": unresolved_segments,
            "overlap_pairs": len(fusion.overlaps),
            "overlap_exact_confirmed": overlap_exact_confirmed,
            "overlap_cross_transform_confirmed": (overlap_cross_transform_confirmed),
            "overlap_near_confirmed": overlap_near_confirmed,
            "overlap_deferred": overlap_deferred,
            "overlap_conflicts": overlap_conflicts,
            "overlap_missing": overlap_missing,
            "diagnostics": list(fusion.diagnostics),
            "invariants": {
                "reference_evidence_forbidden": True,
                "exact_codepoints_without_whitespace": True,
                "or_xor_geometry_only": not bool(plan.membership_units),
                "membership_units_are_typed": True,
                "input_sha256_bound": True,
                "context_sha256_bound": True,
                "unassigned_words_fail_closed": True,
                "replica_conflicts_fail_closed": True,
                "overlap_consensus_categories_typed": True,
                "near_consensus_candidate_preserving": True,
                "deferred_segments_fail_closed": True,
            },
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        (stage / "diagnostics.txt").write_text(
            "\n".join(
                (
                    "stage=2 ocr-evidence-fusion",
                    f"status={fusion.status.value}",
                    f"queue_status={queue.status.value}",
                    f"jobs={len(queue.jobs)}",
                    f"failed_jobs={failed_jobs}",
                    f"unresolved_segments={unresolved_segments}",
                    f"segment_groups={len(fusion.segment_groups)}",
                    "unresolved_segment_groups=" f"{sum(item.unresolved for item in fusion.segment_groups)}",
                    f"unassigned_words={len(fusion.unassigned_word_observations)}",
                    f"replica_conflicts={len(fusion.replica_conflicts)}",
                    f"overlap_exact_confirmed={overlap_exact_confirmed}",
                    "overlap_cross_transform_confirmed=" f"{overlap_cross_transform_confirmed}",
                    f"overlap_near_confirmed={overlap_near_confirmed}",
                    f"overlap_deferred={overlap_deferred}",
                    f"overlap_conflicts={overlap_conflicts}",
                    f"overlap_missing={overlap_missing}",
                    *fusion.diagnostics,
                )
            )
            + "\n",
            encoding="utf-8",
        )


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values),
        encoding="utf-8",
    )


__all__ = ["OcrArtifactWriter"]
