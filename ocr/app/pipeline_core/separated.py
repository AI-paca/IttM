from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from PIL import Image

from app.pipeline_core.native import NativePipelineCore, native_pipeline_core

SEPARATED_STAGES = (
    "preprocess",
    "geometry",
    "topology",
    "find-object",
    "separate-block",
    "ocr-blocks",
    "get-segment",
    "generate-object",
)
ALL_SEPARATED_STAGE_MASK = (1 << len(SEPARATED_STAGES)) - 1


@dataclass(frozen=True)
class SeparatedOcrJob:
    index: int
    bbox: tuple[int, int, int, int]
    object_id: int
    row: int
    column: int
    row_span: int
    column_span: int
    recognition_mode: int


RecognitionResult: TypeAlias = str | tuple[str, int]


class NativeSeparatedSession:
    def __init__(self, image: Image.Image, core: NativePipelineCore | None = None):
        self._core = core or native_pipeline_core()
        if self._core is None:
            raise RuntimeError("Native separated pipeline core is unavailable")
        raster = image.convert("RGB")
        try:
            width, height = raster.size
            self._handle = self._core.separated_begin(
                raster.tobytes(),
                width,
                height,
                width * 3,
                3,
            )
        finally:
            if raster is not image:
                raster.close()
        self._closed = False

    @property
    def jobs(self) -> tuple[SeparatedOcrJob, ...]:
        fields = self._core.separated_job_field
        return tuple(
            SeparatedOcrJob(
                index=index,
                bbox=tuple(fields(self._handle, index, field) for field in range(4)),
                object_id=fields(self._handle, index, 4),
                row=fields(self._handle, index, 5),
                column=fields(self._handle, index, 6),
                row_span=fields(self._handle, index, 7),
                column_span=fields(self._handle, index, 8),
                recognition_mode=fields(self._handle, index, 9),
            )
            for index in range(self._core.separated_job_count(self._handle))
        )

    @property
    def completed_stages(self) -> tuple[str, ...]:
        mask = self._core.separated_stage_mask(self._handle)
        return tuple(stage for index, stage in enumerate(SEPARATED_STAGES) if mask & (1 << index))

    def set_ocr(self, index: int, text: str, confidence_milli: int = 0) -> None:
        self._core.separated_set_ocr(
            self._handle,
            index,
            text,
            confidence_milli,
        )

    def raster(self, job: SeparatedOcrJob) -> Image.Image:
        pixels, width, height, stride, pixel_format = self._core.separated_job_raster(
            self._handle,
            job.index,
        )
        if pixel_format != 3 or stride != width * 3:
            raise RuntimeError("Separated OCR raster is not packed RGB")
        return Image.frombytes("RGB", (width, height), pixels)

    def render(self) -> str:
        return self._core.separated_render(self._handle)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._core.separated_drop(self._handle)

    def __enter__(self) -> "NativeSeparatedSession":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def run_native_separated_pipeline(
    image: Image.Image,
    recognize: Callable[[Image.Image, SeparatedOcrJob], RecognitionResult],
) -> tuple[str, tuple[SeparatedOcrJob, ...], tuple[str, ...]]:
    with NativeSeparatedSession(image) as session:
        jobs = session.jobs
        for job in jobs:
            crop = session.raster(job)
            try:
                result = recognize(crop, job)
            finally:
                crop.close()
            if isinstance(result, tuple):
                text, confidence_milli = result
            else:
                text, confidence_milli = result, 0
            session.set_ocr(job.index, text, confidence_milli)
        markdown = session.render()
        stages = session.completed_stages
        if stages != SEPARATED_STAGES:
            raise RuntimeError("Separated pipeline did not complete every stage: " + ", ".join(stages))
        return markdown, jobs, stages
