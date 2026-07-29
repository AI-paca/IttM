"""Narrow opt-in service boundary for the production sparse page runtime.

This module intentionally does not alter ``convert_service``.  API routing,
PDF text-layer policy, preprocessing, and engine-to-lane selection remain
product decisions; once a caller has a decoded Pillow page and configured OCR
lanes, this service is ready to use.
"""

from __future__ import annotations

from PIL import Image

from app.sparse_pipeline.ocr_queue import OcrLane
from app.sparse_pipeline.runtime import (
    SparsePageResult,
    SparsePipelineRuntime,
    SparseRuntimeConfig,
)


class SparseConvertService:
    """Own the sparse runtime for the lifetime of one document conversion."""

    def __init__(
        self,
        lanes: tuple[OcrLane, ...],
        config: SparseRuntimeConfig | None = None,
    ) -> None:
        self._runtime = SparsePipelineRuntime(lanes, config)

    @property
    def config(self) -> SparseRuntimeConfig:
        return self._runtime.config

    @property
    def closed(self) -> bool:
        return self._runtime.closed

    def __enter__(self) -> SparseConvertService:
        self._runtime.__enter__()
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self._runtime.__exit__(exc_type, exc, traceback)

    def convert_page(
        self,
        image: Image.Image,
        *,
        page_id: str,
    ) -> SparsePageResult:
        return self._runtime.process_page(image, page_id=page_id)

    def close(self) -> None:
        self._runtime.close()


__all__ = ["SparseConvertService"]
