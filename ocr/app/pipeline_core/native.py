from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PIPELINE_CORE_LIBRARY_ENV = "ITTM_PIPELINE_CORE_LIB"
PIPELINE_CORE_ABI_VERSION = 3


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
        version = int(library.ittm_pipeline_abi_version())
        if version != PIPELINE_CORE_ABI_VERSION:
            raise RuntimeError(
                f"Unsupported pipeline core ABI {version}; expected {PIPELINE_CORE_ABI_VERSION}"
            )
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
            raise RuntimeError(
                f"Configured pipeline core does not exist: {configured_path}"
            )
        return NativePipelineCore.load(configured_path)
    for candidate in _library_candidates():
        if candidate.is_file():
            return NativePipelineCore.load(candidate)
    return None
