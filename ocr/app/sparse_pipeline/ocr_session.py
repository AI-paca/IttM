from __future__ import annotations

import concurrent.futures
import threading
import time

from app.sparse_pipeline.block_crops import BlockCropPair
from app.sparse_pipeline.block_planning import BlockPlan
from app.sparse_pipeline.ocr_queue import (
    OcrJobResult,
    OcrJobStatus,
    OcrLane,
    OcrQueueConfig,
    OcrQueueInvariantError,
    OcrQueueResult,
    OcrQueueStatus,
    ParallelOcrQueue,
    _JobSpec,
    _deterministic_lane_shards,
)


class _PersistentShard:
    def __init__(self, lane: OcrLane, shard_index: int) -> None:
        self.lane = lane
        self.shard_index = shard_index
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"ocr-session-{lane.lane_id}-{shard_index}",
        )
        self.worker: object | None = None


class PersistentOcrSession:
    """Keep one thread-affine worker per deterministic lane shard across pages."""

    def __init__(
        self,
        lanes: tuple[OcrLane, ...],
        config: OcrQueueConfig | None = None,
    ) -> None:
        if type(lanes) is not tuple or any(not isinstance(lane, OcrLane) for lane in lanes):
            raise TypeError("lanes must be an immutable OcrLane tuple")
        if not lanes:
            raise ValueError("a persistent OCR session requires at least one lane")
        lane_ids = tuple(lane.lane_id for lane in lanes)
        if len(lane_ids) != len(set(lane_ids)):
            raise ValueError("persistent OCR lane identifiers must be unique")
        self.lanes = lanes
        self._queue = ParallelOcrQueue(config)
        if len(lanes) > self._queue.config.max_lanes:
            raise ValueError("persistent OCR lane count exceeds the queue limit")
        self._states = {
            lane.lane_id: tuple(_PersistentShard(lane, index) for index in range(lane.max_workers)) for lane in lanes
        }
        self._worker_ownership: dict[int, tuple[object, str, int]] = {}
        self._worker_ownership_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._closed = False

    @property
    def config(self) -> OcrQueueConfig:
        return self._queue.config

    def __enter__(self) -> PersistentOcrSession:
        if self._closed:
            raise RuntimeError("persistent OCR session is closed")
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        self.close()

    def run(
        self,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
    ) -> OcrQueueResult:
        with self._run_lock:
            if self._closed:
                raise RuntimeError("persistent OCR session is closed")
            self._queue._validate_inputs(
                plan=plan,
                crops=crops,
                lanes=self.lanes,
            )
            if not plan.blocks:
                return OcrQueueResult((), OcrQueueStatus.COMPLETE, 0, 0)
            self._queue._preflight_inputs(
                plan=plan,
                crops=crops,
                lanes=self.lanes,
            )
            specs = self._queue._build_specs(
                plan=plan,
                crops=crops,
                lanes=self.lanes,
            )
            character_quota = min(
                self.config.max_output_chars,
                self.config.max_total_output_chars // len(specs),
            )
            word_quota = min(
                self.config.max_words,
                self.config.max_total_words // len(specs),
            )
            by_lane = {lane.lane_id: tuple(spec for spec in specs if spec.lane is lane) for lane in self.lanes}
            futures: dict[
                concurrent.futures.Future[tuple[OcrJobResult, ...]],
                tuple[OcrLane, int, tuple[_JobSpec, ...]],
            ] = {}
            for lane in self.lanes:
                shards = _deterministic_lane_shards(
                    lane,
                    by_lane[lane.lane_id],
                )
                for shard_index, shard in enumerate(shards):
                    state = self._states[lane.lane_id][shard_index]
                    future = state.executor.submit(
                        self._run_shard,
                        state,
                        shard,
                        character_quota,
                        word_quota,
                    )
                    futures[future] = (lane, shard_index, shard)
            unordered: list[OcrJobResult] = []
            diagnostics: list[str] = []
            for future in concurrent.futures.as_completed(futures):
                lane, shard_index, shard = futures[future]
                try:
                    unordered.extend(future.result())
                except Exception as exc:
                    unordered.extend(
                        self._queue._failed(
                            spec,
                            exc,
                            elapsed_seconds=0.0,
                        )
                        for spec in shard
                    )
                diagnostics.append(f"lane={lane.lane_id};persistent-shard={shard_index}")
            order = {spec.job_id: spec.index for spec in specs}
            jobs = tuple(sorted(unordered, key=lambda job: order[job.job_id]))
            if tuple(job.job_id for job in jobs) != tuple(spec.job_id for spec in specs):
                raise OcrQueueInvariantError("persistent OCR execution lost or duplicated a job")
            complete = sum(job.status is OcrJobStatus.COMPLETE for job in jobs)
            failed = len(jobs) - complete
            return OcrQueueResult(
                jobs=jobs,
                status=(OcrQueueStatus.COMPLETE if failed == 0 else OcrQueueStatus.PARTIAL),
                complete=complete,
                failed=failed,
                diagnostics=tuple(sorted(diagnostics)),
            )

    def _run_shard(
        self,
        state: _PersistentShard,
        specs: tuple[_JobSpec, ...],
        character_quota: int,
        word_quota: int,
    ) -> tuple[OcrJobResult, ...]:
        results: list[OcrJobResult] = []
        for spec_index, spec in enumerate(specs):
            started = time.perf_counter()
            try:
                worker = self._ensure_worker(state)
                output = worker.recognize(spec.png_bytes)
                self._queue._validate_output(
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
                        lane_id=state.lane.lane_id,
                        resource=state.lane.resource,
                        status=OcrJobStatus.COMPLETE,
                        output=output,
                        error_type=None,
                        error_message=None,
                        elapsed_seconds=time.perf_counter() - started,
                        input_sha256=spec.input_sha256,
                        context_sha256=spec.context_sha256,
                        failure_code=None,
                        capability_id=state.lane.capability_id,
                    )
                )
            except Exception as exc:
                results.append(
                    self._queue._failed(
                        spec,
                        exc,
                        elapsed_seconds=time.perf_counter() - started,
                    )
                )
                if bool(getattr(exc, "worker_poisoned", False)):
                    self._close_worker(state)
                if state.worker is None and not bool(getattr(exc, "retryable", False)):
                    results.extend(
                        self._queue._failed(
                            remaining,
                            exc,
                            elapsed_seconds=0.0,
                        )
                        for remaining in specs[spec_index + 1 :]
                    )
                    break
        return tuple(results)

    def _ensure_worker(self, state: _PersistentShard) -> object:
        if state.worker is not None:
            return state.worker
        worker = state.lane.worker_factory()
        recognize = getattr(worker, "recognize", None)
        if not callable(recognize):
            raise TypeError("worker_factory returned an object without recognize()")
        with self._worker_ownership_lock:
            previous = self._worker_ownership.get(id(worker))
            if previous is not None and previous[0] is worker:
                raise ValueError("worker_factory reused one worker across persistent shards")
            self._worker_ownership[id(worker)] = (
                worker,
                state.lane.lane_id,
                state.shard_index,
            )
        state.worker = worker
        return worker

    @staticmethod
    def _close_worker(state: _PersistentShard) -> None:
        worker = state.worker
        state.worker = None
        close = getattr(worker, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def close(self) -> None:
        with self._run_lock:
            if self._closed:
                return
            self._closed = True
            futures = tuple(
                state.executor.submit(self._close_worker, state) for states in self._states.values() for state in states
            )
            for future in futures:
                try:
                    future.result()
                except Exception:
                    pass
            for states in self._states.values():
                for state in states:
                    state.executor.shutdown(wait=True, cancel_futures=True)


__all__ = ["PersistentOcrSession"]
