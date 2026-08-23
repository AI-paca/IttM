from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PIPELINE_CORE_LIBRARY_ENV = "ITTM_PIPELINE_CORE_LIB"
PIPELINE_CORE_ABI_VERSION = 4


@dataclass(frozen=True)
class NativePipelineCore:
    path: Path
    _library: ctypes.CDLL

    @classmethod
    def load(cls, path: Path) -> "NativePipelineCore":
        resolved = path.expanduser().resolve()
        library = ctypes.CDLL(str(resolved))
        library.ittm_pipeline_abi_version.restype = ctypes.c_uint32
        library.ittm_pipeline_recipe_mask.argtypes = (ctypes.c_uint32,)
        library.ittm_pipeline_recipe_mask.restype = ctypes.c_uint32
        library.ittm_sparse_add_signal.argtypes = (ctypes.c_uint32, ctypes.c_uint32)
        library.ittm_sparse_add_signal.restype = ctypes.c_int32
        library.ittm_is_isolated_heading.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_is_isolated_heading.restype = ctypes.c_uint32
        library.ittm_span_evidence_score.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_span_evidence_score.restype = ctypes.c_int32
        library.ittm_should_replace_primary.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_should_replace_primary.restype = ctypes.c_uint32
        library.ittm_should_drop_text_block.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_should_drop_text_block.restype = ctypes.c_uint32
        library.ittm_separated_begin.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_begin.restype = ctypes.c_uint32
        library.ittm_separated_job_count.argtypes = (ctypes.c_uint32,)
        library.ittm_separated_job_count.restype = ctypes.c_uint32
        library.ittm_separated_job_field.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_job_field.restype = ctypes.c_int32
        library.ittm_separated_set_ocr.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_set_ocr.restype = ctypes.c_int32
        library.ittm_separated_render_length.argtypes = (ctypes.c_uint32,)
        library.ittm_separated_render_length.restype = ctypes.c_uint32
        library.ittm_separated_render_copy.argtypes = (
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        library.ittm_separated_render_copy.restype = ctypes.c_int32
        library.ittm_separated_stage_mask.argtypes = (ctypes.c_uint32,)
        library.ittm_separated_stage_mask.restype = ctypes.c_uint32
        library.ittm_separated_drop.argtypes = (ctypes.c_uint32,)
        library.ittm_separated_drop.restype = ctypes.c_int32
        version = int(library.ittm_pipeline_abi_version())
        if version != PIPELINE_CORE_ABI_VERSION:
            raise RuntimeError(f"Unsupported pipeline core ABI {version}; expected {PIPELINE_CORE_ABI_VERSION}")
        return cls(path=resolved, _library=library)

    def recipe_mask(self, capability_bits: int) -> int:
        return int(self._library.ittm_pipeline_recipe_mask(capability_bits))

    def add_sparse_signal(self, code: int, signal: int) -> int:
        result = int(self._library.ittm_sparse_add_signal(code, signal))
        if result == -2:
            raise ValueError(f"Unknown sparse signal: {signal}")
        if result == -1:
            raise ValueError(f"Unknown sparse code: {code}")
        return result

    def is_isolated_heading(
        self,
        run_rows: int,
        content_chars: int,
        max_line_chars: int,
        bounded_above: bool,
        bounded_below: bool,
    ) -> bool:
        boundary_bits = int(bounded_above) | (int(bounded_below) << 1)
        return bool(
            self._library.ittm_is_isolated_heading(
                run_rows,
                content_chars,
                max_line_chars,
                boundary_bits,
            )
        )

    def span_evidence_score(
        self,
        ocr_confidence_milli: int,
        script_consistency_milli: int,
        context_consistency_milli: int,
        source_agreement: int,
        contradictions: int,
    ) -> int:
        values = (
            ocr_confidence_milli,
            script_consistency_milli,
            context_consistency_milli,
            source_agreement,
            contradictions,
        )
        if any(value < 0 for value in values):
            raise ValueError("Span evidence values must be non-negative")
        return int(self._library.ittm_span_evidence_score(*values))

    def should_replace_primary(
        self,
        primary_chars: int,
        fallback_chars: int,
        primary_tokens: int,
        retained_primary_tokens: int,
    ) -> bool:
        values = (
            primary_chars,
            fallback_chars,
            primary_tokens,
            retained_primary_tokens,
        )
        if any(value < 0 for value in values):
            raise ValueError("Primary replacement values must be non-negative")
        return bool(self._library.ittm_should_replace_primary(*values))

    def should_drop_text_block(
        self,
        candidate_chars: int,
        existing_chars: int,
        shared_tokens: int,
        candidate_tokens: int,
        existing_tokens: int,
        similarity_milli: int,
    ) -> bool:
        values = (
            candidate_chars,
            existing_chars,
            shared_tokens,
            candidate_tokens,
            existing_tokens,
            similarity_milli,
        )
        if any(value < 0 for value in values):
            raise ValueError("Text block deduplication values must be non-negative")
        return bool(self._library.ittm_should_drop_text_block(*values))

    def separated_begin(
        self,
        pixels: bytes,
        width: int,
        height: int,
        stride: int,
        pixel_format: int,
    ) -> int:
        if not pixels:
            raise ValueError("Separated pipeline pixels must not be empty")
        source = ctypes.create_string_buffer(pixels)
        handle = int(
            self._library.ittm_separated_begin(
                source,
                len(pixels),
                width,
                height,
                stride,
                pixel_format,
            )
        )
        if handle == 0:
            raise ValueError("Separated pipeline rejected the raster plane")
        return handle

    def separated_job_count(self, handle: int) -> int:
        return int(self._library.ittm_separated_job_count(handle))

    def separated_job_field(self, handle: int, index: int, field: int) -> int:
        value = int(self._library.ittm_separated_job_field(handle, index, field))
        if value < 0:
            raise ValueError(f"Invalid separated OCR job field: {index}:{field}")
        return value

    def separated_set_ocr(
        self,
        handle: int,
        index: int,
        text: str,
        confidence_milli: int = 0,
    ) -> None:
        encoded = text.encode("utf-8")
        source = ctypes.create_string_buffer(encoded) if encoded else None
        status = int(
            self._library.ittm_separated_set_ocr(
                handle,
                index,
                source,
                len(encoded),
                confidence_milli,
            )
        )
        if status != 0:
            raise ValueError(f"Separated OCR handoff failed with status {status}")

    def separated_render(self, handle: int) -> str:
        length = int(self._library.ittm_separated_render_length(handle))
        if length == 0:
            return ""
        output = ctypes.create_string_buffer(length)
        copied = int(self._library.ittm_separated_render_copy(handle, output, length))
        if copied != length:
            raise RuntimeError(f"Separated renderer copied {copied} bytes; expected {length}")
        return bytes(output.raw[:length]).decode("utf-8")

    def separated_stage_mask(self, handle: int) -> int:
        return int(self._library.ittm_separated_stage_mask(handle))

    def separated_drop(self, handle: int) -> None:
        if int(self._library.ittm_separated_drop(handle)) != 0:
            raise ValueError(f"Unknown separated pipeline handle: {handle}")


def _library_candidates() -> tuple[Path, ...]:
    repo_root = Path(__file__).resolve().parents[3]
    return (
        Path(__file__).with_name("libittm_pipeline_core.so"),
        repo_root / "pipeline-core" / "target" / "release" / "libittm_pipeline_core.so",
        repo_root / "pipeline-core" / "target" / "debug" / "libittm_pipeline_core.so",
    )


@lru_cache(maxsize=1)
def native_pipeline_core() -> NativePipelineCore | None:
    configured = os.environ.get(PIPELINE_CORE_LIBRARY_ENV)
    if configured:
        configured_path = Path(configured)
        if not configured_path.is_file():
            raise RuntimeError(f"Configured pipeline core does not exist: {configured_path}")
        return NativePipelineCore.load(configured_path)
    for candidate in _library_candidates():
        if candidate.is_file():
            return NativePipelineCore.load(candidate)
    return None
