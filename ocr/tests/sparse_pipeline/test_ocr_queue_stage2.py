from __future__ import annotations

import hashlib
import io
import json
import math
import subprocess
import sys
import threading
import time
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Callable

import numpy as np
import pytest
from PIL import Image

from app.sparse_pipeline.block_crops import BlockCropPair, BlockCropper
from app.sparse_pipeline.block_planning import (
    BlockPlan,
    BlockSetAlgebra,
    RecognitionBlock,
)
from app.sparse_pipeline.contracts import Box
from app.sparse_pipeline.crop_enhancement import CropInput
from app.sparse_pipeline.ocr_adapter_contracts import (
    OcrFailureCode,
    OcrOutputGeometry,
)
from app.sparse_pipeline.ocr_queue import (
    OcrEngineOutput,
    OcrInvalidOutputError,
    OcrJobResult,
    OcrJobStatus,
    OcrLane,
    OcrQueueConfig,
    OcrQueueInvariantError,
    OcrQueueLimitError,
    OcrQueueResult,
    OcrQueueStatus,
    OcrResource,
    OcrTransform,
    OcrWord,
    ParallelOcrQueue,
    input_sha256,
)


class _Worker:
    def __init__(self, recognize: Callable[[bytes], object]) -> None:
        self._recognize = recognize

    def recognize(self, png_bytes: bytes) -> object:
        return self._recognize(png_bytes)


def _png_bytes(array: np.ndarray) -> bytes:
    output = io.BytesIO()
    image = Image.fromarray(array, mode="RGB")
    try:
        image.save(output, format="PNG", compress_level=1)
    finally:
        image.close()
    return output.getvalue()


def _ordered(
    source_ids: tuple[str, ...],
    values: set[str],
) -> tuple[str, ...]:
    return tuple(segment_id for segment_id in source_ids if segment_id in values)


def _plan(block_count: int) -> BlockPlan:
    aligned_size = (32, max(12, block_count * 12))
    if block_count == 0:
        return BlockPlan(
            aligned_size=aligned_size,
            source_segment_ids=(),
            blocks=(),
            adjacent_algebra=(),
        )
    source_ids = tuple(f"segment-{index:06d}" for index in range(block_count))
    blocks = []
    for index, segment_id in enumerate(source_ids):
        if index == 0:
            members = (segment_id,)
            context = ()
            top = 0
        else:
            members = (source_ids[index - 1], segment_id)
            context = (source_ids[index - 1],)
            top = (index - 1) * 12
        blocks.append(
            RecognitionBlock(
                block_id=f"block-{index:06d}",
                bbox=Box(
                    0,
                    top,
                    aligned_size[0],
                    min(aligned_size[1], (index + 1) * 12),
                ),
                core_segment_ids=(segment_id,),
                segment_ids=members,
                context_segment_ids=context,
                object_ids=(f"object-{index:06d}",),
            )
        )
    algebra = []
    for first, second in zip(blocks, blocks[1:]):
        first_ids = set(first.segment_ids)
        second_ids = set(second.segment_ids)
        algebra.append(
            BlockSetAlgebra(
                first_block_id=first.block_id,
                second_block_id=second.block_id,
                intersection_segment_ids=_ordered(
                    source_ids, first_ids & second_ids
                ),
                union_segment_ids=_ordered(source_ids, first_ids | second_ids),
                xor_segment_ids=_ordered(source_ids, first_ids ^ second_ids),
                first_only_segment_ids=_ordered(
                    source_ids, first_ids - second_ids
                ),
                second_only_segment_ids=_ordered(
                    source_ids, second_ids - first_ids
                ),
            )
        )
    return BlockPlan(
        aligned_size=aligned_size,
        source_segment_ids=source_ids,
        blocks=tuple(blocks),
        adjacent_algebra=tuple(algebra),
    )


def _fixtures(
    block_count: int,
) -> tuple[BlockPlan, tuple[BlockCropPair, ...], dict[str, tuple[str, OcrTransform]]]:
    plan = _plan(block_count)
    if not plan.blocks:
        return plan, (), {}
    width, height = plan.aligned_size
    rng = np.random.default_rng(20260720 + block_count)
    pixels = rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
    page = CropInput("stage2-page", _png_bytes(pixels))
    crops = BlockCropper().crop(page, aligned_size=plan.aligned_size, plan=plan)
    labels: dict[str, tuple[str, OcrTransform]] = {}
    for crop in crops:
        for transform, payload in (
            (OcrTransform.RAW, crop.raw.png_bytes),
            (OcrTransform.GAMMA, crop.gamma.png_bytes),
        ):
            digest = hashlib.sha256(payload).hexdigest()
            assert digest not in labels
            labels[digest] = (crop.block_id, transform)
    return plan, crops, labels


def _output(text: str = "observed") -> OcrEngineOutput:
    return OcrEngineOutput(
        text=text,
        words=(),
        geometry=OcrOutputGeometry.TEXT_ONLY,
    )


def _lane(
    lane_id: str,
    resource: OcrResource,
    factory: Callable[[], object],
    *,
    max_workers: int = 1,
) -> OcrLane:
    return OcrLane(
        lane_id=lane_id,
        resource=resource,
        max_workers=max_workers,
        worker_factory=factory,
    )


def _config(**overrides: int) -> OcrQueueConfig:
    return replace(OcrQueueConfig(), **overrides)


def test_queue_expands_raw_gamma_blocks_and_lanes_in_canonical_order() -> None:
    plan, crops, labels = _fixtures(2)
    calls: list[tuple[str, str, OcrTransform]] = []
    lock = threading.Lock()
    lanes = []
    for lane_id, resource in (
        ("cpu-a", OcrResource.CPU),
        ("gpu-a", OcrResource.GPU),
        ("cpu-b", OcrResource.CPU),
    ):

        def factory(
            lane_id: str = lane_id,
        ) -> _Worker:
            def recognize(payload: bytes) -> OcrEngineOutput:
                block_id, transform = labels[hashlib.sha256(payload).hexdigest()]
                with lock:
                    calls.append((lane_id, block_id, transform))
                return _output(f"{lane_id}:{block_id}:{transform.value}")

            return _Worker(recognize)

        lanes.append(_lane(lane_id, resource, factory, max_workers=2))

    result = ParallelOcrQueue().run(
        plan=plan,
        crops=crops,
        lanes=tuple(lanes),
    )

    expected = tuple(
        (block.block_id, transform, lane.lane_id)
        for block in plan.blocks
        for transform in (OcrTransform.RAW, OcrTransform.GAMMA)
        for lane in lanes
    )
    assert result.status is OcrQueueStatus.COMPLETE
    assert (result.complete, result.failed) == (len(expected), 0)
    assert tuple(
        (job.block_id, job.transform, job.lane_id) for job in result.jobs
    ) == expected
    assert tuple(job.job_id for job in result.jobs) == tuple(
        f"ocr-job-{index:08d}" for index in range(len(expected))
    )
    assert all(job.status is OcrJobStatus.COMPLETE for job in result.jobs)
    assert all(job.output is not None for job in result.jobs)
    assert all(job.error_type is job.error_message is None for job in result.jobs)
    assert all(math.isfinite(job.elapsed_seconds) and job.elapsed_seconds >= 0.0 for job in result.jobs)
    assert {
        (lane_id, block_id, transform)
        for lane_id, block_id, transform in calls
    } == {
        (lane.lane_id, block.block_id, transform)
        for lane in lanes
        for block in plan.blocks
        for transform in (OcrTransform.RAW, OcrTransform.GAMMA)
    }
    assert tuple(input_sha256(job, crops) for job in result.jobs) == tuple(
        hashlib.sha256(
            (
                crops[block_index].raw.png_bytes
                if transform is OcrTransform.RAW
                else crops[block_index].gamma.png_bytes
            )
        ).hexdigest()
        for block_index in range(len(crops))
        for transform in (OcrTransform.RAW, OcrTransform.GAMMA)
        for _lane_value in lanes
    )


def test_independent_lane_pools_really_start_together_without_global_bottleneck() -> None:
    plan, crops, _ = _fixtures(2)
    barrier = threading.Barrier(4)
    lock = threading.Lock()
    active = {"cpu": 0, "gpu": 0}
    peaks = {"cpu": 0, "gpu": 0}
    thread_ids: dict[str, set[int]] = {"cpu": set(), "gpu": set()}

    def lane_factory(lane_id: str) -> Callable[[], _Worker]:
        def factory() -> _Worker:
            def recognize(_payload: bytes) -> OcrEngineOutput:
                thread_id = threading.get_ident()
                with lock:
                    active[lane_id] += 1
                    peaks[lane_id] = max(peaks[lane_id], active[lane_id])
                    thread_ids[lane_id].add(thread_id)
                try:
                    barrier.wait(timeout=2.0)
                    time.sleep(0.01)
                    return _output(lane_id)
                finally:
                    with lock:
                        active[lane_id] -= 1

            return _Worker(recognize)

        return factory

    lanes = (
        _lane("cpu", OcrResource.CPU, lane_factory("cpu"), max_workers=2),
        _lane("gpu", OcrResource.GPU, lane_factory("gpu"), max_workers=2),
    )
    result = ParallelOcrQueue(
        _config(max_pending_per_lane=2)
    ).run(plan=plan, crops=crops, lanes=lanes)

    assert result.status is OcrQueueStatus.COMPLETE
    assert (result.complete, result.failed) == (8, 0)
    assert peaks == {"cpu": 2, "gpu": 2}
    assert all(len(values) == 2 for values in thread_ids.values())
    assert thread_ids["cpu"].isdisjoint(thread_ids["gpu"])


def test_worker_factory_is_lazy_thread_local_and_reused_on_its_executor_thread() -> None:
    plan, crops, _ = _fixtures(4)
    main_thread = threading.get_ident()
    first_calls = threading.Barrier(2)
    lock = threading.Lock()
    workers: list[dict[str, object]] = []

    def factory() -> _Worker:
        creator = threading.get_ident()
        state: dict[str, object] = {
            "creator": creator,
            "calls": 0,
            "threads": set(),
        }
        with lock:
            workers.append(state)

        def recognize(_payload: bytes) -> OcrEngineOutput:
            current = threading.get_ident()
            assert current == creator
            threads = state["threads"]
            assert isinstance(threads, set)
            threads.add(current)
            state["calls"] = int(state["calls"]) + 1
            if state["calls"] == 1:
                first_calls.wait(timeout=2.0)
            time.sleep(0.002)
            return _output("thread-local")

        return _Worker(recognize)

    result = ParallelOcrQueue(
        _config(max_pending_per_lane=4)
    ).run(
        plan=plan,
        crops=crops,
        lanes=(_lane("cpu", OcrResource.CPU, factory, max_workers=2),),
    )

    assert result.failed == 0
    assert len(workers) == 2
    assert sum(int(worker["calls"]) for worker in workers) == 8
    assert all(worker["creator"] != main_thread for worker in workers)
    assert all(worker["threads"] == {worker["creator"]} for worker in workers)
    assert any(int(worker["calls"]) > 1 for worker in workers)


def test_result_order_is_canonical_even_when_jobs_finish_in_reverse_order() -> None:
    plan, crops, labels = _fixtures(2)
    canonical_payloads = [
        payload
        for crop in crops
        for payload in (crop.raw.png_bytes, crop.gamma.png_bytes)
    ]
    delays = {
        hashlib.sha256(payload).hexdigest(): (len(canonical_payloads) - index) * 0.01
        for index, payload in enumerate(canonical_payloads)
    }
    completed: list[tuple[str, OcrTransform]] = []
    lock = threading.Lock()

    def factory() -> _Worker:
        def recognize(payload: bytes) -> OcrEngineOutput:
            digest = hashlib.sha256(payload).hexdigest()
            time.sleep(delays[digest])
            with lock:
                completed.append(labels[digest])
            return _output(digest[:12])

        return _Worker(recognize)

    result = ParallelOcrQueue(
        _config(max_pending_per_lane=4)
    ).run(
        plan=plan,
        crops=crops,
        lanes=(_lane("cpu", OcrResource.CPU, factory, max_workers=4),),
    )

    canonical = tuple(
        (block.block_id, transform)
        for block in plan.blocks
        for transform in (OcrTransform.RAW, OcrTransform.GAMMA)
    )
    assert tuple((job.block_id, job.transform) for job in result.jobs) == canonical
    assert tuple(completed) != canonical
    assert result.failed == 0


def test_one_worker_job_failure_isolated_while_every_other_job_drains() -> None:
    plan, crops, labels = _fixtures(2)
    target = hashlib.sha256(crops[1].gamma.png_bytes).hexdigest()
    calls: list[tuple[str, str]] = []
    lock = threading.Lock()

    def factory(lane_id: str) -> Callable[[], _Worker]:
        def build() -> _Worker:
            def recognize(payload: bytes) -> OcrEngineOutput:
                digest = hashlib.sha256(payload).hexdigest()
                with lock:
                    calls.append((lane_id, digest))
                if lane_id == "flaky" and digest == target:
                    raise RuntimeError("isolated\n worker failure")
                block_id, transform = labels[digest]
                return _output(f"{lane_id}:{block_id}:{transform.value}")

            return _Worker(recognize)

        return build

    result = ParallelOcrQueue().run(
        plan=plan,
        crops=crops,
        lanes=(
            _lane("stable", OcrResource.CPU, factory("stable"), max_workers=2),
            _lane("flaky", OcrResource.GPU, factory("flaky"), max_workers=2),
        ),
    )

    assert result.status is OcrQueueStatus.PARTIAL
    assert (result.complete, result.failed) == (7, 1)
    assert len(calls) == 8
    failed = tuple(job for job in result.jobs if job.status is OcrJobStatus.FAILED)
    assert len(failed) == 1
    assert (failed[0].block_id, failed[0].transform, failed[0].lane_id) == (
        "block-000001",
        OcrTransform.GAMMA,
        "flaky",
    )
    assert failed[0].output is None
    assert failed[0].error_type == "RuntimeError"
    assert failed[0].error_message == "isolated worker failure"
    assert failed[0].elapsed_seconds >= 0.0


def test_lane_factory_failure_does_not_cancel_an_independent_lane() -> None:
    plan, crops, _ = _fixtures(2)

    def broken_factory() -> object:
        raise RuntimeError("factory unavailable")

    result = ParallelOcrQueue().run(
        plan=plan,
        crops=crops,
        lanes=(
            _lane(
                "healthy",
                OcrResource.CPU,
                lambda: _Worker(lambda _payload: _output("ok")),
                max_workers=2,
            ),
            _lane("broken", OcrResource.GPU, broken_factory, max_workers=2),
        ),
    )

    healthy = tuple(job for job in result.jobs if job.lane_id == "healthy")
    broken = tuple(job for job in result.jobs if job.lane_id == "broken")
    assert len(healthy) == len(broken) == 4
    assert all(job.status is OcrJobStatus.COMPLETE for job in healthy)
    assert all(job.status is OcrJobStatus.FAILED for job in broken)
    assert all(job.error_type == "RuntimeError" for job in broken)
    assert result.status is OcrQueueStatus.PARTIAL


def test_hard_job_and_input_preflight_limits_run_before_any_factory() -> None:
    plan, crops, _ = _fixtures(2)
    factory_calls = 0

    def factory() -> _Worker:
        nonlocal factory_calls
        factory_calls += 1
        return _Worker(lambda _payload: _output())

    lane = _lane("cpu", OcrResource.CPU, factory)
    payloads = tuple(
        payload
        for crop in crops
        for payload in (crop.raw.png_bytes, crop.gamma.png_bytes)
    )
    jobs = len(payloads)
    total_bytes = sum(len(payload) for payload in payloads)
    configs = (
        _config(max_jobs=jobs - 1),
        _config(max_lanes=1, max_jobs=jobs),
        _config(max_job_input_bytes=max(len(payload) for payload in payloads) - 1),
        _config(max_total_input_bytes=total_bytes - 1),
    )
    lane_sets = (
        (lane,),
        (lane, _lane("gpu", OcrResource.GPU, factory)),
        (lane,),
        (lane,),
    )

    for config, lanes in zip(configs, lane_sets):
        with pytest.raises(OcrQueueLimitError):
            ParallelOcrQueue(config).run(plan=plan, crops=crops, lanes=lanes)
    assert factory_calls == 0


def test_job_and_byte_limits_fail_before_spec_allocation_or_hashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, crops, _ = _fixtures(2)
    lane = _lane(
        "cpu",
        OcrResource.CPU,
        lambda: _Worker(lambda _payload: _output()),
    )
    payloads = tuple(
        payload
        for crop in crops
        for payload in (crop.raw.png_bytes, crop.gamma.png_bytes)
    )
    calls = {"build": 0, "hash": 0}

    def forbidden_build(_self: object, **_values: object) -> object:
        calls["build"] += 1
        raise AssertionError("job specs must not be allocated before preflight")

    def forbidden_hash(*_args: object, **_kwargs: object) -> object:
        calls["hash"] += 1
        raise AssertionError("payloads must not be hashed before preflight")

    monkeypatch.setattr(ParallelOcrQueue, "_build_specs", forbidden_build)
    monkeypatch.setattr(
        "app.sparse_pipeline.ocr_queue.hashlib.sha256",
        forbidden_hash,
    )
    configs = (
        _config(max_jobs=len(payloads) - 1),
        _config(max_job_input_bytes=max(map(len, payloads)) - 1),
        _config(max_total_input_bytes=sum(map(len, payloads)) - 1),
    )

    for config in configs:
        with pytest.raises(OcrQueueLimitError):
            ParallelOcrQueue(config).run(
                plan=plan,
                crops=crops,
                lanes=(lane,),
            )
    assert calls == {"build": 0, "hash": 0}


def test_empty_plan_returns_without_lanes_or_worker_factory() -> None:
    plan, crops, _ = _fixtures(0)
    factory_called = False

    def forbidden_factory() -> object:
        nonlocal factory_called
        factory_called = True
        raise AssertionError("empty queue must not create a worker")

    for lanes in (
        (),
        (_lane("cpu", OcrResource.CPU, forbidden_factory),),
    ):
        result = ParallelOcrQueue().run(plan=plan, crops=crops, lanes=lanes)
        assert result == OcrQueueResult(
            jobs=(),
            status=OcrQueueStatus.COMPLETE,
            complete=0,
            failed=0,
        )
    assert factory_called is False


def test_malformed_crop_scope_and_lanes_are_rejected_before_factory() -> None:
    plan, crops, _ = _fixtures(2)
    factory_called = False

    def factory() -> _Worker:
        nonlocal factory_called
        factory_called = True
        return _Worker(lambda _payload: _output())

    lane = _lane("cpu", OcrResource.CPU, factory)
    forged = replace(crops[0], segment_ids=("forged-segment",))
    invalid_calls = (
        {"plan": plan, "crops": tuple(reversed(crops)), "lanes": (lane,)},
        {"plan": plan, "crops": (forged, crops[1]), "lanes": (lane,)},
        {"plan": plan, "crops": list(crops), "lanes": (lane,)},
        {"plan": plan, "crops": crops, "lanes": ()},
        {"plan": plan, "crops": crops, "lanes": (lane, lane)},
    )
    for values in invalid_calls:
        with pytest.raises(OcrQueueInvariantError):
            ParallelOcrQueue().run(**values)  # type: ignore[arg-type]
    with pytest.raises(OcrQueueLimitError, match="pending|worker"):
        ParallelOcrQueue(_config(max_pending_per_lane=1)).run(
            plan=plan,
            crops=crops,
            lanes=(replace(lane, max_workers=2),),
        )
    assert factory_called is False


def test_character_quota_is_per_job_and_finish_order_independent() -> None:
    plan, crops, labels = _fixtures(1)

    def run(raw_delay: float, gamma_delay: float) -> OcrQueueResult:
        delays = {
            ("block-000000", OcrTransform.RAW): raw_delay,
            ("block-000000", OcrTransform.GAMMA): gamma_delay,
        }

        def factory() -> _Worker:
            def recognize(payload: bytes) -> OcrEngineOutput:
                label = labels[hashlib.sha256(payload).hexdigest()]
                time.sleep(delays[label])
                return _output("123456" if label[1] is OcrTransform.RAW else "1234")

            return _Worker(recognize)

        return ParallelOcrQueue(
            _config(
                max_output_chars=100,
                max_total_output_chars=10,
                max_pending_per_lane=2,
            )
        ).run(
            plan=plan,
            crops=crops,
            lanes=(_lane("cpu", OcrResource.CPU, factory, max_workers=2),),
        )

    for result in (run(0.03, 0.0), run(0.0, 0.03)):
        assert tuple(job.status for job in result.jobs) == (
            OcrJobStatus.FAILED,
            OcrJobStatus.COMPLETE,
        )
        assert result.jobs[0].error_type == OcrInvalidOutputError.__name__
        assert "character quota" in (result.jobs[0].error_message or "")
        assert result.status is OcrQueueStatus.PARTIAL


def test_word_quota_and_crop_local_bbox_violations_fail_only_their_jobs() -> None:
    plan, crops, labels = _fixtures(1)
    block = plan.blocks[0]

    def factory() -> _Worker:
        def recognize(payload: bytes) -> OcrEngineOutput:
            _block_id, transform = labels[hashlib.sha256(payload).hexdigest()]
            if transform is OcrTransform.RAW:
                return OcrEngineOutput(
                    text="one two",
                    words=(
                        OcrWord("one", Box(0, 0, 2, 2), 0.9),
                        OcrWord("two", Box(2, 0, 4, 2), 0.8),
                    ),
                )
            return OcrEngineOutput(
                text="outside",
                words=(
                    OcrWord(
                        "outside",
                        Box(0, 0, block.bbox.width + 1, 2),
                        0.7,
                    ),
                ),
            )

        return _Worker(recognize)

    result = ParallelOcrQueue(
        _config(max_words=10, max_total_words=2)
    ).run(
        plan=plan,
        crops=crops,
        lanes=(_lane("cpu", OcrResource.CPU, factory),),
    )

    assert result.failed == 2
    assert all(job.error_type == OcrInvalidOutputError.__name__ for job in result.jobs)
    assert "word quota" in (result.jobs[0].error_message or "")
    assert "outside its crop" in (result.jobs[1].error_message or "")


def test_malformed_worker_return_fails_one_job_without_hidden_fallback() -> None:
    plan, crops, labels = _fixtures(1)

    def factory() -> _Worker:
        def recognize(payload: bytes) -> object:
            _block_id, transform = labels[hashlib.sha256(payload).hexdigest()]
            return object() if transform is OcrTransform.RAW else _output("gamma")

        return _Worker(recognize)

    result = ParallelOcrQueue().run(
        plan=plan,
        crops=crops,
        lanes=(_lane("cpu", OcrResource.CPU, factory),),
    )

    assert tuple(job.status for job in result.jobs) == (
        OcrJobStatus.FAILED,
        OcrJobStatus.COMPLETE,
    )
    assert result.jobs[0].error_type == OcrInvalidOutputError.__name__
    assert "OcrEngineOutput" in (result.jobs[0].error_message or "")


def test_word_box_text_must_exactly_match_ordered_words_after_compaction() -> None:
    plan, crops, labels = _fixtures(1)

    def factory() -> _Worker:
        def recognize(payload: bytes) -> OcrEngineOutput:
            _block_id, transform = labels[hashlib.sha256(payload).hexdigest()]
            words = (
                OcrWord("alpha", Box(0, 0, 5, 2), 0.9),
                OcrWord("beta", Box(5, 0, 9, 2), 0.8),
            )
            return OcrEngineOutput(
                text=(" alpha\n beta " if transform is OcrTransform.RAW else "beta alpha"),
                words=words,
                geometry=OcrOutputGeometry.WORD_BOXES,
            )

        return _Worker(recognize)

    result = ParallelOcrQueue().run(
        plan=plan,
        crops=crops,
        lanes=(_lane("cpu", OcrResource.CPU, factory),),
    )

    assert tuple(job.status for job in result.jobs) == (
        OcrJobStatus.COMPLETE,
        OcrJobStatus.FAILED,
    )
    assert result.jobs[1].failure_code is OcrFailureCode.INVALID_OUTPUT
    assert "exactly represented" in (result.jobs[1].error_message or "")


def test_empty_word_box_output_is_a_typed_recognition_miss() -> None:
    plan, crops, _ = _fixtures(1)
    result = ParallelOcrQueue().run(
        plan=plan,
        crops=crops,
        lanes=(
            _lane(
                "cpu",
                OcrResource.CPU,
                lambda: _Worker(
                    lambda _payload: OcrEngineOutput(
                        text="",
                        words=(),
                        geometry=OcrOutputGeometry.WORD_BOXES,
                    )
                ),
            ),
        ),
    )

    assert result.failed == 2
    assert all(job.status is OcrJobStatus.FAILED for job in result.jobs)
    assert all(job.failure_code is OcrFailureCode.RECOGNITION_MISS for job in result.jobs)


def test_pending_window_and_running_workers_stay_bounded_per_lane() -> None:
    plan, crops, _ = _fixtures(4)
    lock = threading.Lock()
    active = 0
    peak_active = 0

    def factory() -> _Worker:
        def recognize(_payload: bytes) -> OcrEngineOutput:
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
            try:
                time.sleep(0.005)
                return _output()
            finally:
                with lock:
                    active -= 1

        return _Worker(recognize)

    result = ParallelOcrQueue(
        _config(max_pending_per_lane=2)
    ).run(
        plan=plan,
        crops=crops,
        lanes=(_lane("cpu", OcrResource.CPU, factory, max_workers=1),),
    )

    assert result.failed == 0
    assert peak_active == 1
    assert result.diagnostics == ("lane=cpu;workers=1;peak-pending=1",)


def test_factory_reusing_one_worker_across_threads_is_detected_per_job() -> None:
    plan, crops, _ = _fixtures(2)
    shared = _Worker(lambda _payload: (time.sleep(0.03), _output("shared"))[1])

    result = ParallelOcrQueue(
        _config(max_pending_per_lane=4)
    ).run(
        plan=plan,
        crops=crops,
        lanes=(
            _lane("cpu", OcrResource.CPU, lambda: shared, max_workers=2),
        ),
    )

    assert result.failed >= 1
    assert any(
        job.error_type == OcrInvalidOutputError.__name__
        and "reused one worker" in (job.error_message or "")
        for job in result.jobs
    )
    assert len(result.jobs) == 4


def test_factory_reusing_one_singleton_worker_across_two_lanes_is_rejected() -> None:
    plan, crops, _ = _fixtures(1)
    shared = _Worker(lambda _payload: (time.sleep(0.01), _output("shared"))[1])
    lanes = (
        _lane("cpu-a", OcrResource.CPU, lambda: shared),
        _lane("cpu-b", OcrResource.CPU, lambda: shared),
    )

    result = ParallelOcrQueue().run(plan=plan, crops=crops, lanes=lanes)

    assert len(result.jobs) == 4
    assert result.failed >= 1
    assert any(
        job.error_type == OcrInvalidOutputError.__name__
        and "worker" in (job.error_message or "")
        and ("lane" in (job.error_message or "") or "reused" in (job.error_message or ""))
        for job in result.jobs
    )


def test_stateful_worker_outputs_are_delay_independent_via_deterministic_shards() -> None:
    plan, crops, labels = _fixtures(4)
    canonical_labels = tuple(
        labels[hashlib.sha256(payload).hexdigest()]
        for crop in crops
        for payload in (crop.raw.png_bytes, crop.gamma.png_bytes)
    )

    def run(delayed: tuple[str, OcrTransform]) -> tuple[str, ...]:
        first_calls = threading.Barrier(2)

        def factory() -> _Worker:
            history: list[str] = []

            def recognize(payload: bytes) -> OcrEngineOutput:
                label = labels[hashlib.sha256(payload).hexdigest()]
                history.append(f"{label[0]}:{label[1].value}")
                if len(history) == 1:
                    first_calls.wait(timeout=2.0)
                if label == delayed:
                    time.sleep(0.04)
                return _output("/".join(history))

            return _Worker(recognize)

        result = ParallelOcrQueue(
            _config(max_pending_per_lane=8)
        ).run(
            plan=plan,
            crops=crops,
            lanes=(_lane("cpu", OcrResource.CPU, factory, max_workers=2),),
        )
        assert result.failed == 0
        return tuple(job.output.text for job in result.jobs if job.output is not None)

    assert run(canonical_labels[0]) == run(canonical_labels[1])


def test_raw_gamma_pair_is_atomic_on_one_worker_shard_for_every_block() -> None:
    plan, crops, labels = _fixtures(4)
    lock = threading.Lock()
    next_worker_id = 0
    calls: list[tuple[int, tuple[str, OcrTransform]]] = []

    def factory() -> _Worker:
        nonlocal next_worker_id
        with lock:
            worker_id = next_worker_id
            next_worker_id += 1

        def recognize(payload: bytes) -> OcrEngineOutput:
            label = labels[hashlib.sha256(payload).hexdigest()]
            with lock:
                calls.append((worker_id, label))
            if label[1] is OcrTransform.RAW:
                time.sleep(0.005)
            return _output(f"worker-{worker_id}")

        return _Worker(recognize)

    result = ParallelOcrQueue(
        _config(max_pending_per_lane=4)
    ).run(
        plan=plan,
        crops=crops,
        lanes=(_lane("cpu", OcrResource.CPU, factory, max_workers=2),),
    )

    assert result.failed == 0
    owner = {label: worker_id for worker_id, label in calls}
    for block in plan.blocks:
        assert owner[(block.block_id, OcrTransform.RAW)] == owner[
            (block.block_id, OcrTransform.GAMMA)
        ]
    for worker_id in {item[0] for item in calls}:
        sequence = tuple(label for owner_id, label in calls if owner_id == worker_id)
        assert len(sequence) % 2 == 0
        for raw, gamma in zip(sequence[::2], sequence[1::2]):
            assert raw == (gamma[0], OcrTransform.RAW)
            assert gamma[1] is OcrTransform.GAMMA


def test_malformed_exception_failure_code_is_isolated_and_typed_engine_error() -> None:
    plan, crops, labels = _fixtures(1)
    raw_digest = hashlib.sha256(crops[0].raw.png_bytes).hexdigest()

    class MalformedCodeError(RuntimeError):
        code = "not-an-ocr-failure-code"

    def factory() -> _Worker:
        def recognize(payload: bytes) -> OcrEngineOutput:
            digest = hashlib.sha256(payload).hexdigest()
            if digest == raw_digest:
                raise MalformedCodeError("bad third-party exception metadata")
            assert labels[digest][1] is OcrTransform.GAMMA
            return _output("gamma-survives")

        return _Worker(recognize)

    result = ParallelOcrQueue().run(
        plan=plan,
        crops=crops,
        lanes=(_lane("cpu", OcrResource.CPU, factory),),
    )

    assert tuple(job.status for job in result.jobs) == (
        OcrJobStatus.FAILED,
        OcrJobStatus.COMPLETE,
    )
    assert result.jobs[0].error_type == "MalformedCodeError"
    assert result.jobs[0].failure_code is OcrFailureCode.ENGINE_ERROR
    assert result.jobs[1].output is not None
    assert result.jobs[1].output.text == "gamma-survives"


def test_job_results_bind_exact_input_and_raw_context_sha256() -> None:
    plan, crops, _ = _fixtures(2)
    lanes = (
        _lane(
            "cpu",
            OcrResource.CPU,
            lambda: _Worker(lambda _payload: _output()),
        ),
        _lane(
            "gpu",
            OcrResource.GPU,
            lambda: _Worker(lambda _payload: _output()),
        ),
    )

    result = ParallelOcrQueue().run(plan=plan, crops=crops, lanes=lanes)

    for job in result.jobs:
        block_index = int(job.block_id.rsplit("-", 1)[1])
        crop = crops[block_index]
        expected_input = hashlib.sha256(
            crop.raw.png_bytes
            if job.transform is OcrTransform.RAW
            else crop.gamma.png_bytes
        ).hexdigest()
        expected_context = hashlib.sha256(crop.raw.png_bytes).hexdigest()
        assert job.input_sha256 == expected_input
        assert job.context_sha256 == expected_context
        assert input_sha256(job, crops) == job.input_sha256
        if job.transform is OcrTransform.RAW:
            assert job.input_sha256 == job.context_sha256


@pytest.mark.parametrize(
    "field",
    (
        "max_lanes",
        "max_jobs",
        "max_job_input_bytes",
        "max_total_input_bytes",
        "max_output_chars",
        "max_total_output_chars",
        "max_words",
        "max_total_words",
        "max_pending_per_lane",
    ),
)
def test_queue_config_rejects_nonpositive_or_boolean_limits(field: str) -> None:
    with pytest.raises(ValueError, match="positive|limit"):
        replace(OcrQueueConfig(), **{field: 0})
    with pytest.raises(ValueError, match="positive|limit"):
        replace(OcrQueueConfig(), **{field: True})


@pytest.mark.parametrize("confidence", (-0.1, 1.1, math.nan, math.inf, True))
def test_word_rejects_invalid_confidence(confidence: object) -> None:
    with pytest.raises(ValueError, match="confidence"):
        OcrWord("word", Box(0, 0, 1, 1), confidence)  # type: ignore[arg-type]


def test_value_objects_reject_mutable_or_inconsistent_payloads() -> None:
    with pytest.raises(ValueError, match="text"):
        OcrWord("", Box(0, 0, 1, 1), 0.5)
    with pytest.raises(ValueError, match="tuple|immutable"):
        OcrEngineOutput("text", [])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="lane|identifier"):
        _lane("../unsafe", OcrResource.CPU, lambda: object())
    with pytest.raises(ValueError, match="resource"):
        OcrLane("lane", "cpu", 1, lambda: object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_workers|positive"):
        _lane("lane", OcrResource.CPU, lambda: object(), max_workers=0)
    with pytest.raises(ValueError, match="callable|factory"):
        OcrLane("lane", OcrResource.CPU, 1, object())  # type: ignore[arg-type]
    complete = OcrJobResult(
        job_id="ocr-job-00000000",
        block_id="block-000000",
        transform=OcrTransform.RAW,
        lane_id="cpu",
        resource=OcrResource.CPU,
        status=OcrJobStatus.COMPLETE,
        output=_output(),
        error_type=None,
        error_message=None,
        elapsed_seconds=0.0,
    )
    with pytest.raises(ValueError, match="complete|error"):
        replace(complete, error_type="RuntimeError", error_message="bad")
    with pytest.raises(ValueError, match="elapsed"):
        replace(complete, elapsed_seconds=math.nan)
    with pytest.raises(ValueError, match="counter"):
        OcrQueueResult((complete,), OcrQueueStatus.COMPLETE, 0, 1)
    with pytest.raises(ValueError, match="status"):
        OcrQueueResult((complete,), OcrQueueStatus.PARTIAL, 1, 0)


def test_queue_results_and_lane_contracts_are_frozen() -> None:
    plan, crops, _ = _fixtures(1)
    lane = _lane(
        "cpu",
        OcrResource.CPU,
        lambda: _Worker(lambda _payload: _output()),
    )
    result = ParallelOcrQueue().run(plan=plan, crops=crops, lanes=(lane,))

    with pytest.raises(FrozenInstanceError):
        lane.max_workers = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.jobs = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.jobs[0].status = OcrJobStatus.FAILED  # type: ignore[misc]


def test_stage2_import_does_not_load_real_ocr_or_gpu_runtimes() -> None:
    repository = Path(__file__).resolve().parents[3]
    code = """
import json
import sys
sys.path.insert(0, 'ocr')
import app.sparse_pipeline.ocr_queue
banned_roots = {
    'cv2', 'easyocr', 'kornia', 'onnxruntime', 'paddle', 'paddleocr',
    'pytesseract', 'tensorflow', 'tesserocr', 'torch',
}
banned = sorted(
    name for name in sys.modules
    if name.split('.', 1)[0] in banned_roots
    or name.startswith('app.engines')
    or name.startswith('app.recognition')
)
print(json.dumps(banned))
raise SystemExit(bool(banned))
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(completed.stdout) == []
