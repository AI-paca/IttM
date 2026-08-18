from __future__ import annotations

import concurrent.futures
import hashlib
import math
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from app.sparse_pipeline.ocr_adapter_contracts import (
    OcrFailureCode,
    OcrOutputGeometry,
    OcrRecognitionMissError,
    OcrResource,
)
from app.sparse_pipeline.block_crops import BlockCropPair
from app.sparse_pipeline.block_planning import BlockPlan
from app.sparse_pipeline.contracts import Box

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MAX_ERROR_MESSAGE = 512


class OcrQueueInvariantError(ValueError):
    """Raised before work starts when Stage 5 evidence is inconsistent."""


class OcrQueueLimitError(RuntimeError):
    """Raised before work starts when a configured queue budget is exceeded."""


class OcrInvalidOutputError(ValueError):
    """Converted into one failed job when an OCR worker violates its boundary."""

    code = OcrFailureCode.INVALID_OUTPUT


class OcrTransform(str, Enum):
    RAW = "raw"
    GAMMA = "gamma"
    CONTEXTUAL_COMPOSITE = "contextual/composite"
    SOURCE_PLACEMENT_FALLBACK = "source-placement-fallback"


class OcrJobStatus(str, Enum):
    COMPLETE = "complete"
    FAILED = "failed"


class OcrQueueStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True)
class OcrWord:
    text: str
    bbox: Box
    confidence: float

    def __post_init__(self) -> None:
        if type(self.text) is not str or not self.text:
            raise ValueError("OCR word text must be a non-empty observed string")
        if not isinstance(self.bbox, Box):
            raise ValueError("OCR word bbox must be a crop-local Box")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise ValueError("OCR word confidence must be between zero and one")


@dataclass(frozen=True)
class OcrEngineOutput:
    text: str
    words: tuple[OcrWord, ...]
    geometry: OcrOutputGeometry = OcrOutputGeometry.WORD_BOXES

    def __post_init__(self) -> None:
        if type(self.text) is not str:
            raise ValueError("OCR engine text must be an observed string")
        if type(self.words) is not tuple or any(
            not isinstance(word, OcrWord) for word in self.words
        ):
            raise ValueError("OCR engine words must be an immutable OcrWord tuple")
        if not isinstance(self.geometry, OcrOutputGeometry):
            raise ValueError("OCR engine output geometry is invalid")
        if self.geometry is OcrOutputGeometry.TEXT_ONLY and self.words:
            raise ValueError("text-only OCR output cannot contain word boxes")
        if self.geometry is OcrOutputGeometry.WORD_BOXES and (
            bool(self.text.strip()) != bool(self.words)
        ):
            raise OcrInvalidOutputError(
                "bbox OCR output text and observed words disagree"
            )
        if self.geometry is OcrOutputGeometry.WORD_BOXES and self.words and (
            "".join(character for character in self.text if not character.isspace())
            != "".join(
                character
                for word in self.words
                for character in word.text
                if not character.isspace()
            )
        ):
            raise OcrInvalidOutputError(
                "bbox OCR text is not exactly represented by its observed words"
            )


class OcrWorker(Protocol):
    def recognize(self, png_bytes: bytes) -> OcrEngineOutput: ...


WorkerFactory = Callable[[], OcrWorker]


@dataclass(frozen=True)
class OcrLane:
    lane_id: str
    resource: OcrResource
    max_workers: int
    worker_factory: WorkerFactory
    capability_id: str = ""

    def __post_init__(self) -> None:
        if type(self.lane_id) is not str or not _SAFE_ID.fullmatch(self.lane_id):
            raise ValueError("OCR lane identifier is invalid")
        if not isinstance(self.resource, OcrResource):
            raise ValueError("OCR lane resource is invalid")
        if type(self.max_workers) is not int or self.max_workers < 1:
            raise ValueError("OCR lane max_workers must be a positive integer")
        if not callable(self.worker_factory):
            raise ValueError("OCR lane worker_factory must be callable")
        if self.capability_id == "":
            object.__setattr__(self, "capability_id", self.lane_id)
        if type(self.capability_id) is not str or not _SAFE_ID.fullmatch(self.capability_id):
            raise ValueError("OCR lane capability identifier is invalid")


@dataclass(frozen=True)
class OcrQueueConfig:
    max_lanes: int = 32
    max_jobs: int = 100_000
    max_job_input_bytes: int = 64 * 1024 * 1024
    max_total_input_bytes: int = 512 * 1024 * 1024
    max_output_chars: int = 256_000
    max_total_output_chars: int = 16_000_000
    max_words: int = 100_000
    max_total_words: int = 1_000_000
    max_pending_per_lane: int = 64

    def __post_init__(self) -> None:
        values = (
            self.max_lanes,
            self.max_jobs,
            self.max_job_input_bytes,
            self.max_total_input_bytes,
            self.max_output_chars,
            self.max_total_output_chars,
            self.max_words,
            self.max_total_words,
            self.max_pending_per_lane,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise ValueError("OCR queue limits must be positive integers")


@dataclass(frozen=True)
class OcrJobResult:
    job_id: str
    block_id: str
    transform: OcrTransform
    lane_id: str
    resource: OcrResource
    status: OcrJobStatus
    output: OcrEngineOutput | None
    error_type: str | None
    error_message: str | None
    elapsed_seconds: float
    input_sha256: str = ""
    context_sha256: str = ""
    failure_code: OcrFailureCode | None = None
    capability_id: str = "unspecified"

    def __post_init__(self) -> None:
        identifiers = (self.job_id, self.block_id, self.lane_id)
        if any(type(value) is not str or not value for value in identifiers):
            raise ValueError("OCR job identifiers must not be empty")
        if not isinstance(self.transform, OcrTransform):
            raise ValueError("OCR job transform is invalid")
        if not isinstance(self.resource, OcrResource):
            raise ValueError("OCR job resource is invalid")
        if not isinstance(self.status, OcrJobStatus):
            raise ValueError("OCR job status is invalid")
        if (
            isinstance(self.elapsed_seconds, bool)
            or not isinstance(self.elapsed_seconds, (int, float))
            or not math.isfinite(float(self.elapsed_seconds))
            or self.elapsed_seconds < 0.0
        ):
            raise ValueError("OCR job elapsed time must be finite and non-negative")
        for name, value in (
            ("input_sha256", self.input_sha256),
            ("context_sha256", self.context_sha256),
        ):
            if value and (
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"OCR job {name} must be lowercase SHA-256")
        if self.capability_id == "unspecified":
            object.__setattr__(self, "capability_id", self.lane_id)
        if type(self.capability_id) is not str or not _SAFE_ID.fullmatch(
            self.capability_id
        ):
            raise ValueError("OCR job capability identifier is invalid")
        if self.status is OcrJobStatus.COMPLETE:
            if not isinstance(self.output, OcrEngineOutput):
                raise ValueError("a complete OCR job requires an engine output")
            if self.error_type is not None or self.error_message is not None:
                raise ValueError("a complete OCR job cannot contain an error")
            if self.failure_code is not None:
                raise ValueError("a complete OCR job cannot contain a failure code")
        elif (
            self.output is not None
            or type(self.error_type) is not str
            or not self.error_type
            or type(self.error_message) is not str
            or (
                self.failure_code is not None
                and not isinstance(self.failure_code, OcrFailureCode)
            )
        ):
            raise ValueError("a failed OCR job requires a typed error and no output")


@dataclass(frozen=True)
class OcrQueueResult:
    jobs: tuple[OcrJobResult, ...]
    status: OcrQueueStatus
    complete: int
    failed: int
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.jobs) is not tuple or any(
            not isinstance(job, OcrJobResult) for job in self.jobs
        ):
            raise ValueError("OCR queue jobs must be an immutable tuple")
        if not isinstance(self.status, OcrQueueStatus):
            raise ValueError("OCR queue status is invalid")
        if type(self.complete) is not int or type(self.failed) is not int:
            raise ValueError("OCR queue counters must be integers")
        actual_complete = sum(
            job.status is OcrJobStatus.COMPLETE for job in self.jobs
        )
        actual_failed = len(self.jobs) - actual_complete
        if (self.complete, self.failed) != (actual_complete, actual_failed):
            raise ValueError("OCR queue counters disagree with jobs")
        expected_status = (
            OcrQueueStatus.COMPLETE if actual_failed == 0 else OcrQueueStatus.PARTIAL
        )
        if self.status is not expected_status:
            raise ValueError("OCR queue status disagrees with jobs")
        if type(self.diagnostics) is not tuple or any(
            type(item) is not str or not item for item in self.diagnostics
        ):
            raise ValueError("OCR queue diagnostics must be an immutable string tuple")


@dataclass(frozen=True)
class _JobSpec:
    index: int
    job_id: str
    block_id: str
    transform: OcrTransform
    lane: OcrLane
    png_bytes: bytes
    crop_size: tuple[int, int]
    input_sha256: str
    context_sha256: str


class ParallelOcrQueue:
    """Run independent OCR lanes concurrently with bounded per-lane pending work."""

    def __init__(self, config: OcrQueueConfig | None = None) -> None:
        if config is not None and not isinstance(config, OcrQueueConfig):
            raise TypeError("config must be an OcrQueueConfig")
        self.config = config or OcrQueueConfig()

    def run(
        self,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        lanes: tuple[OcrLane, ...],
    ) -> OcrQueueResult:
        self._validate_inputs(plan=plan, crops=crops, lanes=lanes)
        if not plan.blocks:
            return OcrQueueResult((), OcrQueueStatus.COMPLETE, 0, 0)
        self._preflight_inputs(plan=plan, crops=crops, lanes=lanes)
        specs = self._build_specs(plan=plan, crops=crops, lanes=lanes)
        character_quota = min(
            self.config.max_output_chars,
            self.config.max_total_output_chars // len(specs),
        )
        word_quota = min(
            self.config.max_words,
            self.config.max_total_words // len(specs),
        )
        by_lane = {
            lane.lane_id: tuple(spec for spec in specs if spec.lane is lane)
            for lane in lanes
        }
        unordered: list[OcrJobResult] = []
        diagnostics: list[str] = []
        worker_ownership: dict[int, tuple[object, str, int]] = {}
        worker_ownership_lock = threading.Lock()
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(lanes),
            thread_name_prefix="ocr-lane-controller",
        ) as controllers:
            futures = {
                controllers.submit(
                    self._run_lane,
                    lane,
                    by_lane[lane.lane_id],
                    worker_ownership=worker_ownership,
                    worker_ownership_lock=worker_ownership_lock,
                    character_quota=character_quota,
                    word_quota=word_quota,
                ): lane
                for lane in lanes
            }
            for future in concurrent.futures.as_completed(futures):
                lane = futures[future]
                try:
                    results, peak_pending = future.result()
                except Exception as exc:  # defensive isolation around a whole lane
                    results = tuple(
                        self._failed(spec, exc, elapsed_seconds=0.0)
                        for spec in by_lane[lane.lane_id]
                    )
                    peak_pending = 0
                unordered.extend(results)
                diagnostics.append(
                    f"lane={lane.lane_id};workers={lane.max_workers};peak-pending={peak_pending}"
                )
        order = {spec.job_id: spec.index for spec in specs}
        jobs = tuple(sorted(unordered, key=lambda job: order[job.job_id]))
        if tuple(job.job_id for job in jobs) != tuple(spec.job_id for spec in specs):
            raise OcrQueueInvariantError("parallel OCR execution lost or duplicated a job")
        complete = sum(job.status is OcrJobStatus.COMPLETE for job in jobs)
        failed = len(jobs) - complete
        return OcrQueueResult(
            jobs=jobs,
            status=(
                OcrQueueStatus.COMPLETE
                if failed == 0
                else OcrQueueStatus.PARTIAL
            ),
            complete=complete,
            failed=failed,
            diagnostics=tuple(sorted(diagnostics)),
        )

    def _validate_inputs(
        self,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        lanes: tuple[OcrLane, ...],
    ) -> None:
        if not isinstance(plan, BlockPlan):
            raise OcrQueueInvariantError("plan must be a BlockPlan")
        if type(crops) is not tuple or any(
            not isinstance(crop, BlockCropPair) for crop in crops
        ):
            raise OcrQueueInvariantError("crops must be an immutable BlockCropPair tuple")
        if type(lanes) is not tuple or any(not isinstance(lane, OcrLane) for lane in lanes):
            raise OcrQueueInvariantError("lanes must be an immutable OcrLane tuple")
        if len(lanes) > self.config.max_lanes:
            raise OcrQueueLimitError(
                f"lane count exceeds configured limit {self.config.max_lanes}"
            )
        lane_ids = tuple(lane.lane_id for lane in lanes)
        if len(lane_ids) != len(set(lane_ids)):
            raise OcrQueueInvariantError("OCR lane identifiers must be unique")
        if plan.blocks and not lanes:
            raise OcrQueueInvariantError("a non-empty block plan requires OCR lanes")
        if len(crops) != len(plan.blocks):
            raise OcrQueueInvariantError("block plan and crop count disagree")
        for block, crop in zip(plan.blocks, crops):
            if (
                crop.block_id != block.block_id
                or crop.bbox != block.bbox
                or crop.segment_ids != block.segment_ids
            ):
                raise OcrQueueInvariantError(
                    "block plan and crop provenance disagree"
                )
        if any(
            lane.max_workers > self.config.max_pending_per_lane for lane in lanes
        ):
            raise OcrQueueLimitError(
                "lane workers exceed the configured pending-work bound"
            )

    def _build_specs(
        self,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        lanes: tuple[OcrLane, ...],
    ) -> tuple[_JobSpec, ...]:
        specs: list[_JobSpec] = []
        for block, crop in zip(plan.blocks, crops):
            context_sha256 = hashlib.sha256(crop.raw.png_bytes).hexdigest()
            candidates = (
                (OcrTransform.RAW, crop.raw.png_bytes),
                (OcrTransform.GAMMA, crop.gamma.png_bytes),
            )
            for transform, png_bytes in candidates:
                for lane in lanes:
                    index = len(specs)
                    specs.append(
                        _JobSpec(
                            index=index,
                            job_id=f"ocr-job-{index:08d}",
                            block_id=block.block_id,
                            transform=transform,
                            lane=lane,
                            png_bytes=png_bytes,
                            crop_size=(block.bbox.width, block.bbox.height),
                            input_sha256=hashlib.sha256(png_bytes).hexdigest(),
                            context_sha256=context_sha256,
                        )
                    )
        return tuple(specs)

    def _preflight_inputs(
        self,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        lanes: tuple[OcrLane, ...],
    ) -> None:
        job_count = 2 * len(plan.blocks) * len(lanes)
        if job_count > self.config.max_jobs:
            raise OcrQueueLimitError(
                f"job count exceeds configured limit {self.config.max_jobs}"
            )
        oversized = next(
            (
                (crop.block_id, transform)
                for crop in crops
                for transform, payload in (
                    (OcrTransform.RAW, crop.raw.png_bytes),
                    (OcrTransform.GAMMA, crop.gamma.png_bytes),
                )
                if len(payload) > self.config.max_job_input_bytes
            ),
            None,
        )
        if oversized is not None:
            raise OcrQueueLimitError(
                f"{oversized[0]} {oversized[1].value} exceeds the input byte limit"
            )
        total_bytes = len(lanes) * sum(
            len(crop.raw.png_bytes) + len(crop.gamma.png_bytes) for crop in crops
        )
        if total_bytes > self.config.max_total_input_bytes:
            raise OcrQueueLimitError(
                "aggregate OCR job input bytes exceed the configured limit"
            )

    def _run_lane(
        self,
        lane: OcrLane,
        specs: tuple[_JobSpec, ...],
        *,
        worker_ownership: dict[int, tuple[object, str, int]],
        worker_ownership_lock: threading.Lock,
        character_quota: int,
        word_quota: int,
    ) -> tuple[tuple[OcrJobResult, ...], int]:
        shards = _deterministic_lane_shards(lane, specs)

        def run_shard(
            shard_index: int,
            shard: tuple[_JobSpec, ...],
        ) -> tuple[OcrJobResult, ...]:
            try:
                worker = lane.worker_factory()
                recognize = getattr(worker, "recognize", None)
                if not callable(recognize):
                    raise OcrInvalidOutputError(
                        "worker_factory returned an object without recognize()"
                    )
                with worker_ownership_lock:
                    previous = worker_ownership.get(id(worker))
                    if previous is not None and previous[0] is worker:
                        raise OcrInvalidOutputError(
                            "worker_factory reused one worker across deterministic shards"
                        )
                    worker_ownership[id(worker)] = (
                        worker,
                        lane.lane_id,
                        shard_index,
                    )
            except Exception as exc:
                return tuple(
                    self._failed(spec, exc, elapsed_seconds=0.0) for spec in shard
                )
            results: list[OcrJobResult] = []
            for spec in shard:
                started = time.perf_counter()
                try:
                    output = worker.recognize(spec.png_bytes)
                    self._validate_output(
                        output,
                        crop_size=spec.crop_size,
                        character_quota=character_quota,
                        word_quota=word_quota,
                    )
                    results.append(
                        OcrJobResult(
                            job_id=spec.job_id,
                            block_id=spec.block_id,
                            transform=spec.transform,
                            lane_id=lane.lane_id,
                            resource=lane.resource,
                            status=OcrJobStatus.COMPLETE,
                            output=output,
                            error_type=None,
                            error_message=None,
                            elapsed_seconds=time.perf_counter() - started,
                            input_sha256=spec.input_sha256,
                            context_sha256=spec.context_sha256,
                            failure_code=None,
                            capability_id=lane.capability_id,
                        )
                    )
                except Exception as exc:
                    results.append(
                        self._failed(
                            spec,
                            exc,
                            elapsed_seconds=time.perf_counter() - started,
                        )
                    )
            return tuple(results)

        results: list[OcrJobResult] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(shards),
            thread_name_prefix=f"ocr-{lane.lane_id}",
        ) as workers:
            futures = tuple(
                workers.submit(run_shard, index, shard)
                for index, shard in enumerate(shards)
            )
            for future in concurrent.futures.as_completed(futures):
                results.extend(future.result())
        return tuple(results), len(shards)

    @staticmethod
    def _validate_output(
        output: object,
        *,
        crop_size: tuple[int, int],
        character_quota: int,
        word_quota: int,
    ) -> None:
        if not isinstance(output, OcrEngineOutput):
            raise OcrInvalidOutputError(
                "recognize() must return an OcrEngineOutput"
            )
        if output.geometry is OcrOutputGeometry.WORD_BOXES and not output.text.strip() and not output.words:
            raise OcrRecognitionMissError("bbox OCR output is empty")
        if output.geometry is OcrOutputGeometry.WORD_BOXES and (
            bool(output.text.strip()) != bool(output.words)
        ):
            raise OcrInvalidOutputError(
                "bbox OCR output text and attributable words disagree"
            )
        if output.geometry is OcrOutputGeometry.WORD_BOXES and (
            "".join(character for character in output.text if not character.isspace())
            != "".join(
                character
                for word in output.words
                for character in word.text
                if not character.isspace()
            )
        ):
            raise OcrInvalidOutputError(
                "bbox OCR text is not exactly represented by its observed words"
            )
        if output.geometry is OcrOutputGeometry.TEXT_ONLY and not output.text.strip():
            raise OcrRecognitionMissError("text-only OCR output is empty")
        character_count = len(output.text) + sum(len(word.text) for word in output.words)
        if character_count > character_quota:
            raise OcrInvalidOutputError(
                "OCR output exceeds its deterministic character quota"
            )
        if len(output.words) > word_quota:
            raise OcrInvalidOutputError(
                "OCR output exceeds its deterministic word quota"
            )
        canvas = Box(0, 0, crop_size[0], crop_size[1])
        if any(word.bbox.intersection(canvas) != word.bbox for word in output.words):
            raise OcrInvalidOutputError("OCR returned a word outside its crop")

    @staticmethod
    def _failed(
        spec: _JobSpec,
        exc: Exception,
        *,
        elapsed_seconds: float,
    ) -> OcrJobResult:
        message = " ".join(str(exc).split())[:_MAX_ERROR_MESSAGE]
        failure_code = getattr(exc, "code", OcrFailureCode.ENGINE_ERROR)
        if not isinstance(failure_code, OcrFailureCode):
            failure_code = OcrFailureCode.ENGINE_ERROR
        return OcrJobResult(
            job_id=spec.job_id,
            block_id=spec.block_id,
            transform=spec.transform,
            lane_id=spec.lane.lane_id,
            resource=spec.lane.resource,
            status=OcrJobStatus.FAILED,
            output=None,
            error_type=type(exc).__name__,
            error_message=message,
            elapsed_seconds=elapsed_seconds,
            input_sha256=spec.input_sha256,
            context_sha256=spec.context_sha256,
            failure_code=failure_code,
            capability_id=spec.lane.capability_id,
        )


def _deterministic_lane_shards(
    lane: OcrLane,
    specs: tuple[_JobSpec, ...],
) -> tuple[tuple[_JobSpec, ...], ...]:
    block_pairs = tuple(
        specs[index : index + 2] for index in range(0, len(specs), 2)
    )
    if any(
        len(pair) != 2
        or pair[0].block_id != pair[1].block_id
        or pair[0].transform is not OcrTransform.RAW
        or pair[1].transform is not OcrTransform.GAMMA
        for pair in block_pairs
    ):
        raise OcrQueueInvariantError(
            "each lane must receive atomic RAW/GAMMA block pairs"
        )
    if not block_pairs:
        return ()
    shard_count = min(lane.max_workers, len(block_pairs))
    return tuple(
        tuple(spec for pair in block_pairs[index::shard_count] for spec in pair)
        for index in range(shard_count)
    )


def input_sha256(job: OcrJobResult, crops: tuple[BlockCropPair, ...]) -> str:
    """Return the immutable candidate digest without storing payloads in results."""

    crop_by_id = {crop.block_id: crop for crop in crops}
    crop = crop_by_id.get(job.block_id)
    if crop is None:
        raise OcrQueueInvariantError("job references an unknown block crop")
    payload = (
        crop.raw.png_bytes
        if job.transform in (
            OcrTransform.RAW,
            OcrTransform.CONTEXTUAL_COMPOSITE,
        )
        else crop.gamma.png_bytes
    )
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "OcrEngineOutput",
    "OcrInvalidOutputError",
    "OcrJobResult",
    "OcrJobStatus",
    "OcrLane",
    "OcrQueueConfig",
    "OcrQueueInvariantError",
    "OcrQueueLimitError",
    "OcrQueueResult",
    "OcrQueueStatus",
    "OcrResource",
    "OcrTransform",
    "OcrWord",
    "OcrWorker",
    "ParallelOcrQueue",
    "WorkerFactory",
    "input_sha256",
]
