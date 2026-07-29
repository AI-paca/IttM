from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

from app.pipeline_core.contracts import (
    PipelineStageTrace,
    RecognizedSegment,
    RecognitionBatch,
)

SegmentHandle = TypeVar("SegmentHandle")
RECOGNITION_COUNTERS = (
    "chunks",
    "cards_found",
    "tables_found",
    "table_cells",
)


def recognize_segments(
    segments: Iterable[SegmentHandle],
    recognize: Callable[[SegmentHandle], RecognizedSegment],
) -> RecognitionBatch:
    """Execute recognition strictly through its image-free output contract."""

    inputs = tuple(segments)
    outputs = tuple(recognize(segment) for segment in inputs)
    totals = tuple(
        (
            name,
            sum(output.counter(name) for output in outputs),
        )
        for name in RECOGNITION_COUNTERS
    )
    flags = tuple(sorted({flag for output in outputs for flag in output.flags}))
    return RecognitionBatch(
        segments=outputs,
        totals=totals,
        flags=flags,
        trace=PipelineStageTrace(
            version=1,
            stage="recognize_segments",
            input_count=len(inputs),
            output_count=len(outputs),
            flags=flags,
        ),
    )
