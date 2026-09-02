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
SEPARATED_LANGUAGE_PROFILES = (
    "rus+eng",
    "rus",
    "eng",
    "chi_sim",
    "ell",
    "equ",
)
SEPARATED_TRANSFORMS = ("raw", "gamma-dark")


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
    object_kind: int
    languages: str
    transform: str
    depth: int
    logical_row_count: int
    logical_column_count: int
    grammar_milli: int
    superseded: bool


@dataclass(frozen=True)
class SeparatedJobSegment:
    index: int
    source_index: int
    source_bbox: tuple[int, int, int, int]
    cell: tuple[int, int, int, int]
    crop_bbox: tuple[int, int, int, int] | None
    placement_source_bbox: tuple[int, int, int, int] | None


@dataclass(frozen=True)
class SeparatedOcrWord:
    text: str
    bbox: tuple[int, int, int, int]
    confidence_milli: int


@dataclass(frozen=True)
class SeparatedRecognition:
    text: str
    confidence_milli: int
    words: tuple[SeparatedOcrWord, ...] = ()


@dataclass(frozen=True)
class SeparatedTopologySlot:
    x: tuple[int, int]
    code: int
    empty: bool


@dataclass(frozen=True)
class SeparatedTopologyRow:
    index: int
    y: tuple[int, int]
    source_matrix_rows: tuple[int, ...]
    slots: tuple[SeparatedTopologySlot, ...]


@dataclass(frozen=True)
class SeparatedObject:
    index: int
    bbox: tuple[int, int, int, int]
    object_kind: int
    segment_indexes: tuple[int, ...]
    reading_index: int
    row_start: int
    row_stop: int
    column_start: int
    column_stop: int


@dataclass(frozen=True)
class SeparatedBlock:
    index: int
    bbox: tuple[int, int, int, int]
    object_id: int
    segment_indexes: tuple[int, ...]
    dyadic_mask: bool
    matrix_window: tuple[int, int, int, int]
    logical_scope_shape: tuple[int, int]


@dataclass(frozen=True)
class SeparatedBlockStageInput:
    metadata: tuple[int, ...]
    pixels: bytes
    width: int
    height: int
    stride: int


@dataclass(frozen=True)
class SeparatedOcrStageInput:
    block_index: int
    languages: str
    transform: str
    text: str
    grammar_milli: int
    words: tuple[SeparatedOcrWord, ...] = ()
    words_in_source_space: bool = True


@dataclass(frozen=True)
class SeparatedRecognizedSegment:
    index: int
    object_id: int
    object_kind: int
    cell: tuple[int, int, int, int]
    source_segment_indexes: tuple[int, ...]
    text: str


RecognitionResult: TypeAlias = str | tuple[str, int] | SeparatedRecognition


class NativeSeparatedSession:
    def __init__(
        self,
        image: Image.Image,
        core: NativePipelineCore | None = None,
        *,
        start_ocr: bool = True,
    ):
        self._core = core or native_pipeline_core()
        if self._core is None:
            raise RuntimeError("Native separated pipeline core is unavailable")
        raster = image.convert("RGB")
        try:
            width, height = raster.size
            begin = (
                self._core.separated_begin
                if start_ocr
                else self._core.separated_plan_begin
            )
            self._handle = begin(
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

    @classmethod
    def from_separate_blocks(
        cls,
        blocks: tuple[SeparatedBlockStageInput, ...],
        core: NativePipelineCore | None = None,
    ) -> NativeSeparatedSession:
        if not blocks:
            raise ValueError("Separate-block checkpoint contains no OCR blocks")
        value = cls.__new__(cls)
        value._core = core or native_pipeline_core()
        if value._core is None:
            raise RuntimeError("Native separated pipeline core is unavailable")
        value._handle = value._core.separated_import_blocks_begin()
        value._closed = False
        try:
            for block in blocks:
                value._core.separated_import_block(
                    value._handle,
                    block.metadata,
                    block.pixels,
                    block.width,
                    block.height,
                    block.stride,
                )
        except BaseException:
            value.close()
            raise
        return value

    @classmethod
    def from_recognized_segments(
        cls,
        segments: tuple[SeparatedRecognizedSegment, ...],
        core: NativePipelineCore | None = None,
    ) -> NativeSeparatedSession:
        if not segments:
            raise ValueError("Get-segment checkpoint contains no segments")
        value = cls.__new__(cls)
        value._core = core or native_pipeline_core()
        if value._core is None:
            raise RuntimeError("Native separated pipeline core is unavailable")
        value._handle = value._core.separated_import_segments_begin()
        value._closed = False
        try:
            for segment in segments:
                value._core.separated_import_segment(
                    value._handle,
                    segment.object_id,
                    segment.object_kind,
                    segment.cell,
                    segment.source_segment_indexes,
                    segment.text,
                )
            value._core.separated_import_segments_finish(value._handle)
        except BaseException:
            value.close()
            raise
        return value

    def start_ocr(self) -> None:
        self._core.separated_start_ocr(self._handle)

    def import_ocr(self, jobs: tuple[SeparatedOcrStageInput, ...]) -> None:
        if not jobs:
            raise ValueError("OCR checkpoint contains no selected jobs")
        for job in jobs:
            profile = SEPARATED_LANGUAGE_PROFILES.index(job.languages)
            transform = SEPARATED_TRANSFORMS.index(job.transform)
            index = self._core.separated_import_ocr_job(
                self._handle,
                job.block_index,
                profile,
                transform,
                job.words_in_source_space,
                job.text,
                job.grammar_milli,
            )
            for word in job.words:
                self._core.separated_add_ocr_word(
                    self._handle,
                    index,
                    word.text,
                    word.bbox,
                    word.confidence_milli,
                )
        self._core.separated_import_ocr_finish(self._handle)

    def run_get_segment(self) -> None:
        self._core.separated_run_get_segment(self._handle)

    @property
    def recognized_segments(self) -> tuple[SeparatedRecognizedSegment, ...]:
        values = []
        field = self._core.separated_segment_field
        for index in range(self._core.separated_segment_count(self._handle)):
            source_count = field(self._handle, index, 6)
            values.append(
                SeparatedRecognizedSegment(
                    index=index,
                    object_id=field(self._handle, index, 0),
                    object_kind=field(self._handle, index, 1),
                    cell=tuple(field(self._handle, index, value) for value in range(2, 6)),
                    source_segment_indexes=tuple(
                        self._core.separated_segment_source(self._handle, index, source)
                        for source in range(source_count)
                    ),
                    text=self._core.separated_segment_text(self._handle, index),
                )
            )
        return tuple(values)

    @property
    def route_id(self) -> int:
        return self._core.route_id()

    @property
    def job_count(self) -> int:
        return self._core.separated_job_count(self._handle)

    def job(self, index: int) -> SeparatedOcrJob:
        fields = self._core.separated_job_field
        language_profile = fields(self._handle, index, 11)
        transform = fields(self._handle, index, 12)
        return SeparatedOcrJob(
            index=index,
            bbox=tuple(fields(self._handle, index, field) for field in range(4)),
            object_id=fields(self._handle, index, 4),
            row=fields(self._handle, index, 5),
            column=fields(self._handle, index, 6),
            row_span=fields(self._handle, index, 7),
            column_span=fields(self._handle, index, 8),
            recognition_mode=fields(self._handle, index, 9),
            object_kind=fields(self._handle, index, 10),
            languages=SEPARATED_LANGUAGE_PROFILES[language_profile],
            transform=SEPARATED_TRANSFORMS[transform],
            depth=fields(self._handle, index, 13),
            logical_row_count=fields(self._handle, index, 14),
            logical_column_count=fields(self._handle, index, 15),
            grammar_milli=fields(self._handle, index, 16),
            superseded=bool(fields(self._handle, index, 17)),
        )

    def job_segments(self, index: int) -> tuple[SeparatedJobSegment, ...]:
        count = self._core.separated_job_segment_count(self._handle, index)
        values = []
        for segment_index in range(count):
            fields = tuple(
                self._core.separated_job_segment_field(
                    self._handle,
                    index,
                    segment_index,
                    field,
                )
                for field in range(18)
            )
            placed = bool(fields[16])
            values.append(
                SeparatedJobSegment(
                    index=segment_index,
                    source_index=fields[17],
                    source_bbox=fields[0:4],
                    cell=fields[4:8],
                    crop_bbox=fields[8:12] if placed else None,
                    placement_source_bbox=fields[12:16] if placed else None,
                )
            )
        return tuple(values)

    @property
    def jobs(self) -> tuple[SeparatedOcrJob, ...]:
        return tuple(self.job(index) for index in range(self.job_count))

    def text(self, index: int) -> str:
        return self._core.separated_job_text(self._handle, index)

    @property
    def objects(self) -> tuple[SeparatedObject, ...]:
        field = self._core.separated_object_field
        values = []
        for index in range(self._core.separated_object_count(self._handle)):
            segment_count = field(self._handle, index, 5)
            values.append(
                SeparatedObject(
                    index=index,
                    bbox=tuple(field(self._handle, index, value) for value in range(4)),
                    object_kind=field(self._handle, index, 4),
                    segment_indexes=tuple(
                        self._core.separated_object_segment(self._handle, index, segment)
                        for segment in range(segment_count)
                    ),
                    reading_index=field(self._handle, index, 6),
                    row_start=field(self._handle, index, 7),
                    row_stop=field(self._handle, index, 8),
                    column_start=field(self._handle, index, 9),
                    column_stop=field(self._handle, index, 10),
                )
            )
        return tuple(values)

    @property
    def blocks(self) -> tuple[SeparatedBlock, ...]:
        field = self._core.separated_block_field
        values = []
        for index in range(self._core.separated_block_count(self._handle)):
            segment_count = field(self._handle, index, 5)
            values.append(
                SeparatedBlock(
                    index=index,
                    bbox=tuple(field(self._handle, index, value) for value in range(4)),
                    object_id=field(self._handle, index, 4),
                    segment_indexes=tuple(
                        self._core.separated_block_segment(self._handle, index, segment)
                        for segment in range(segment_count)
                    ),
                    dyadic_mask=bool(field(self._handle, index, 6)),
                    matrix_window=tuple(field(self._handle, index, value) for value in range(7, 11)),
                    logical_scope_shape=(
                        field(self._handle, index, 11),
                        field(self._handle, index, 12),
                    ),
                )
            )
        return tuple(values)

    @property
    def topology(self) -> tuple[SeparatedTopologyRow, ...]:
        rows = []
        for row_index in range(self._core.separated_topology_row_count(self._handle)):
            field = self._core.separated_topology_row_field
            source_count = field(self._handle, row_index, 2)
            slot_count = field(self._handle, row_index, 3)
            rows.append(
                SeparatedTopologyRow(
                    index=row_index,
                    y=(field(self._handle, row_index, 0), field(self._handle, row_index, 1)),
                    source_matrix_rows=tuple(
                        self._core.separated_topology_source_row(
                            self._handle,
                            row_index,
                            source_index,
                        )
                        for source_index in range(source_count)
                    ),
                    slots=tuple(
                        SeparatedTopologySlot(
                            x=(
                                self._core.separated_topology_slot_field(
                                    self._handle, row_index, slot_index, 0
                                ),
                                self._core.separated_topology_slot_field(
                                    self._handle, row_index, slot_index, 1
                                ),
                            ),
                            code=self._core.separated_topology_slot_field(
                                self._handle, row_index, slot_index, 2
                            ),
                            empty=bool(
                                self._core.separated_topology_slot_field(
                                    self._handle, row_index, slot_index, 3
                                )
                            ),
                        )
                        for slot_index in range(slot_count)
                    ),
                )
            )
        return tuple(rows)

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

    def add_ocr_word(self, index: int, word: SeparatedOcrWord) -> None:
        self._core.separated_add_ocr_word(
            self._handle,
            index,
            word.text,
            word.bbox,
            word.confidence_milli,
        )

    def raster(self, job: SeparatedOcrJob) -> Image.Image:
        pixels, width, height, stride, pixel_format = self._core.separated_job_raster(
            self._handle,
            job.index,
        )
        if pixel_format != 3 or stride != width * 3:
            raise RuntimeError("Separated OCR raster is not packed RGB")
        return Image.frombytes("RGB", (width, height), pixels)

    def block_raster(self, block: SeparatedBlock) -> Image.Image:
        pixels, width, height, stride, pixel_format = self._core.separated_block_raster(
            self._handle,
            block.index,
        )
        if pixel_format != 3 or stride != width * 3:
            raise RuntimeError("Separated raw block raster is not packed RGB")
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
        jobs = []
        index = 0
        while index < session.job_count:
            job = session.job(index)
            jobs.append(job)
            crop = session.raster(job)
            try:
                result = recognize(crop, job)
            finally:
                crop.close()
            words = ()
            if isinstance(result, SeparatedRecognition):
                text = result.text
                confidence_milli = result.confidence_milli
                words = result.words
            elif isinstance(result, tuple):
                text, confidence_milli = result
            else:
                text, confidence_milli = result, 0
            for word in words:
                session.add_ocr_word(job.index, word)
            session.set_ocr(job.index, text, confidence_milli)
            index += 1
        markdown = session.render()
        stages = session.completed_stages
        if stages != SEPARATED_STAGES:
            raise RuntimeError("Separated pipeline did not complete every stage: " + ", ".join(stages))
        return markdown, tuple(jobs), stages
