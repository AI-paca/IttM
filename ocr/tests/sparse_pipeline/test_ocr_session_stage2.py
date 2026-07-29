from __future__ import annotations

import hashlib
import io
import threading
import time
from collections.abc import Callable

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
from app.sparse_pipeline.ocr_adapter_contracts import OcrOutputGeometry
from app.sparse_pipeline.ocr_queue import (
    OcrEngineOutput,
    OcrJobStatus,
    OcrLane,
    OcrQueueResult,
    OcrQueueStatus,
    OcrResource,
    OcrTransform,
)
from app.sparse_pipeline.ocr_session import PersistentOcrSession


class _Worker:
    def __init__(
        self,
        recognize: Callable[[bytes], OcrEngineOutput],
        close: Callable[[], None] | None = None,
    ) -> None:
        self._recognize = recognize
        self._close = close

    def recognize(self, png_bytes: bytes) -> OcrEngineOutput:
        return self._recognize(png_bytes)

    def close(self) -> None:
        if self._close is not None:
            self._close()


class _PoisonedWorkerError(RuntimeError):
    worker_poisoned = True
    retryable = True


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
    source_ids = tuple(f"segment-{index:06d}" for index in range(block_count))
    blocks = []
    for index, segment_id in enumerate(source_ids):
        previous = source_ids[index - 1 : index]
        blocks.append(
            RecognitionBlock(
                block_id=f"block-{index:06d}",
                bbox=Box(
                    0,
                    max(0, (index - 1) * 12),
                    aligned_size[0],
                    min(aligned_size[1], (index + 1) * 12),
                ),
                core_segment_ids=(segment_id,),
                segment_ids=previous + (segment_id,),
                context_segment_ids=previous,
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
                    source_ids,
                    first_ids & second_ids,
                ),
                union_segment_ids=_ordered(source_ids, first_ids | second_ids),
                xor_segment_ids=_ordered(source_ids, first_ids ^ second_ids),
                first_only_segment_ids=_ordered(
                    source_ids,
                    first_ids - second_ids,
                ),
                second_only_segment_ids=_ordered(
                    source_ids,
                    second_ids - first_ids,
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
    seed: int,
) -> tuple[
    BlockPlan,
    tuple[BlockCropPair, ...],
    dict[str, tuple[str, OcrTransform]],
]:
    plan = _plan(block_count)
    width, height = plan.aligned_size
    rng = np.random.default_rng(seed)
    pixels = rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
    page = CropInput(f"session-page-{seed}", _png_bytes(pixels))
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
    factory: Callable[[], object],
    *,
    max_workers: int,
    resource: OcrResource = OcrResource.CPU,
) -> OcrLane:
    return OcrLane(
        lane_id=lane_id,
        resource=resource,
        max_workers=max_workers,
        worker_factory=factory,
    )


def _assert_complete(result: OcrQueueResult, job_count: int) -> None:
    assert result.status is OcrQueueStatus.COMPLETE
    assert (result.complete, result.failed) == (job_count, 0)
    assert all(job.status is OcrJobStatus.COMPLETE for job in result.jobs)


def test_workers_load_once_and_remain_thread_affine_across_pages() -> None:
    first_plan, first_crops, _ = _fixtures(4, 20260721)
    second_plan, second_crops, _ = _fixtures(4, 20260722)
    lock = threading.Lock()
    states: list[dict[str, object]] = []

    def factory() -> _Worker:
        state: dict[str, object] = {
            "creator": threading.get_ident(),
            "calls": [],
            "close_threads": [],
        }
        with lock:
            states.append(state)

        def recognize(_payload: bytes) -> OcrEngineOutput:
            state["calls"].append(threading.get_ident())  # type: ignore[union-attr]
            return _output()

        def close() -> None:
            state["close_threads"].append(  # type: ignore[union-attr]
                threading.get_ident()
            )

        return _Worker(recognize, close)

    session = PersistentOcrSession(
        (_lane("persistent", factory, max_workers=2),)
    )
    first = session.run(plan=first_plan, crops=first_crops)
    second = session.run(plan=second_plan, crops=second_crops)

    _assert_complete(first, 8)
    _assert_complete(second, 8)
    assert len(states) == 2
    assert all(len(state["calls"]) == 8 for state in states)
    assert all(
        set(state["calls"]) == {state["creator"]}  # type: ignore[arg-type]
        for state in states
    )
    session.close()
    assert all(state["close_threads"] == [state["creator"]] for state in states)


def test_each_persistent_shard_keeps_raw_gamma_pairs_atomic() -> None:
    plan, crops, labels = _fixtures(5, 20260723)
    lock = threading.Lock()
    events_by_thread: dict[int, list[tuple[str, OcrTransform]]] = {}

    def factory() -> _Worker:
        owner = threading.get_ident()
        with lock:
            events_by_thread[owner] = []

        def recognize(payload: bytes) -> OcrEngineOutput:
            assert threading.get_ident() == owner
            event = labels[hashlib.sha256(payload).hexdigest()]
            with lock:
                events_by_thread[owner].append(event)
            return _output()

        return _Worker(recognize)

    with PersistentOcrSession(
        (_lane("atomic", factory, max_workers=2),)
    ) as session:
        result = session.run(plan=plan, crops=crops)

    _assert_complete(result, 10)
    assert len(events_by_thread) == 2
    pairs = []
    for events in events_by_thread.values():
        assert len(events) % 2 == 0
        for raw, gamma in zip(events[::2], events[1::2]):
            assert raw[0] == gamma[0]
            assert (raw[1], gamma[1]) == (
                OcrTransform.RAW,
                OcrTransform.GAMMA,
            )
            pairs.append(raw[0])
    assert sorted(pairs) == sorted(block.block_id for block in plan.blocks)


def test_independent_persistent_lanes_execute_concurrently() -> None:
    plan, crops, _ = _fixtures(1, 20260724)
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = 0
    peak = 0
    thread_ids: set[int] = set()

    def factory() -> _Worker:
        first_call = True

        def recognize(_payload: bytes) -> OcrEngineOutput:
            nonlocal active, first_call, peak
            thread_id = threading.get_ident()
            with lock:
                active += 1
                peak = max(peak, active)
                thread_ids.add(thread_id)
            try:
                if first_call:
                    first_call = False
                    barrier.wait(timeout=2.0)
                time.sleep(0.01)
                return _output()
            finally:
                with lock:
                    active -= 1

        return _Worker(recognize)

    lanes = (
        _lane("cpu", factory, max_workers=1),
        _lane(
            "gpu",
            factory,
            max_workers=1,
            resource=OcrResource.GPU,
        ),
    )
    with PersistentOcrSession(lanes) as session:
        result = session.run(plan=plan, crops=crops)

    _assert_complete(result, 4)
    assert peak == 2
    assert len(thread_ids) == 2


def test_poisoned_worker_is_replaced_without_disturbing_other_shard() -> None:
    plan, crops, _ = _fixtures(2, 20260725)
    lock = threading.Lock()
    states: list[dict[str, object]] = []

    def factory() -> _Worker:
        with lock:
            poison = not states
            state: dict[str, object] = {
                "owner": threading.get_ident(),
                "poison": poison,
                "calls": 0,
                "closed": 0,
            }
            states.append(state)

        def recognize(_payload: bytes) -> OcrEngineOutput:
            assert threading.get_ident() == state["owner"]
            state["calls"] = int(state["calls"]) + 1
            if state["poison"] and state["calls"] == 1:
                raise _PoisonedWorkerError("decoder state is poisoned")
            return _output()

        def close() -> None:
            assert threading.get_ident() == state["owner"]
            state["closed"] = int(state["closed"]) + 1

        return _Worker(recognize, close)

    session = PersistentOcrSession(
        (_lane("replace", factory, max_workers=2),)
    )
    result = session.run(plan=plan, crops=crops)

    assert result.status is OcrQueueStatus.PARTIAL
    assert (result.complete, result.failed) == (3, 1)
    failed = tuple(job for job in result.jobs if job.status is OcrJobStatus.FAILED)
    assert len(failed) == 1
    assert failed[0].transform is OcrTransform.RAW
    assert failed[0].error_type == "_PoisonedWorkerError"
    failed_block_jobs = tuple(
        job for job in result.jobs if job.block_id == failed[0].block_id
    )
    assert tuple(job.status for job in failed_block_jobs) == (
        OcrJobStatus.FAILED,
        OcrJobStatus.COMPLETE,
    )
    assert len(states) == 3
    poisoned = next(state for state in states if state["poison"])
    assert (poisoned["calls"], poisoned["closed"]) == (1, 1)
    owner_counts = {
        owner: sum(state["calls"] for state in states if state["owner"] == owner)
        for owner in {state["owner"] for state in states}
    }
    assert sorted(owner_counts.values()) == [2, 2]
    assert sum(state["closed"] for state in states) == 1

    session.close()
    assert all(state["closed"] == 1 for state in states)


def test_close_is_idempotent_and_closed_session_rejects_new_work() -> None:
    plan, crops, _ = _fixtures(1, 20260726)
    calls = {"factory": 0, "close": 0}

    def factory() -> _Worker:
        calls["factory"] += 1
        return _Worker(
            lambda _payload: _output(),
            lambda: calls.__setitem__("close", calls["close"] + 1),
        )

    session = PersistentOcrSession(
        (_lane("closed", factory, max_workers=1),)
    )
    _assert_complete(session.run(plan=plan, crops=crops), 2)
    session.close()
    session.close()

    assert calls == {"factory": 1, "close": 1}
    with pytest.raises(RuntimeError, match="session is closed"):
        session.run(plan=plan, crops=crops)
    with pytest.raises(RuntimeError, match="session is closed"):
        session.__enter__()
    assert calls == {"factory": 1, "close": 1}
