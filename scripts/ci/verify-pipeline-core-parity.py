#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OCR_ROOT = REPO_ROOT / "ocr"
if str(OCR_ROOT) not in sys.path:
    sys.path.insert(0, str(OCR_ROOT))

from app.layout.sparse_codes import SPARSE_CODE_COMPONENTS, SPARSE_SIGNALS, add_sparse_signal
from app.pipeline_core import PipelineCapabilities, recipe_for

STAGE_BITS = {
    "align": 1 << 0,
    "segment": 1 << 1,
    "project_sparse": 1 << 2,
    "recognize_segments": 1 << 3,
    "select_language_candidate": 1 << 4,
    "lexical_correction": 1 << 5,
    "group_structures": 1 << 6,
    "render_markdown": 1 << 7,
}


def capability_bits(capabilities: PipelineCapabilities) -> int:
    return (
        int(capabilities.trusted_text)
        | (int(capabilities.provides_layout) << 1)
        | (int(capabilities.provides_markdown) << 2)
        | (int(capabilities.needs_language_retry) << 3)
    )


def recipe_mask(capabilities: PipelineCapabilities) -> int:
    return sum(STAGE_BITS[stage] for stage in recipe_for(capabilities).stages)


def verify(library_path: Path) -> None:
    library = ctypes.CDLL(str(library_path))
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

    assert library.ittm_pipeline_abi_version() == 3
    for code in SPARSE_CODE_COMPONENTS:
        for signal in SPARSE_SIGNALS:
            assert library.ittm_sparse_add_signal(code, signal) == add_sparse_signal(code, signal)
    assert library.ittm_sparse_add_signal(1, 1) == -2
    assert library.ittm_sparse_add_signal(1, SPARSE_SIGNALS[0]) == -1

    for rows in range(4):
        for chars in (0, 2, 6, 89, 90):
            for line_chars in (0, 6, 89, 90):
                for boundaries in range(4):
                    expected = int(
                        0 < rows <= 2
                        and chars >= 6
                        and line_chars < 90
                        and boundaries == 3
                    )
                    assert (
                        library.ittm_is_isolated_heading(
                            rows,
                            chars,
                            line_chars,
                            boundaries,
                        )
                        == expected
                    )

    for confidence in (0, 1, 500, 999, 1000, 1001):
        for script in (0, 500, 1000, 1001):
            for context in (0, 1000):
                for agreement in (0, 1, 4, 5):
                    for contradictions in (0, 1, 4, 5):
                        expected = (
                            min(confidence, 1000) * 35
                            + min(script, 1000) * 20
                            + min(context, 1000) * 15
                            + min(agreement, 4) * 7500
                            - min(contradictions, 4) * 15000
                        )
                        assert (
                            library.ittm_span_evidence_score(
                                confidence,
                                script,
                                context,
                                agreement,
                                contradictions,
                            )
                            == expected
                        )

    for bits in range(16):
        capabilities = PipelineCapabilities(
            trusted_text=bool(bits & 1),
            provides_layout=bool(bits & 2),
            provides_markdown=bool(bits & 4),
            needs_language_retry=bool(bits & 8),
        )
        assert library.ittm_pipeline_recipe_mask(capability_bits(capabilities)) == recipe_mask(capabilities)

    for primary_chars in (0, 9, 10, 100, 179):
        for fallback_chars in (0, 79, 80, 139, 140, 179, 180, 252):
            for primary_tokens in (0, 1, 5, 10):
                for retained_tokens in (0, 1, 4, 5, 8, 10):
                    expected = int(
                        fallback_chars >= 80
                        if primary_chars < 10
                        else (
                            primary_tokens > 0
                            and retained_tokens * 5 >= primary_tokens * 4
                            and fallback_chars >= 180
                            and fallback_chars * 5 >= primary_chars * 7
                        )
                    )
                    assert (
                        library.ittm_should_replace_primary(
                            primary_chars,
                            fallback_chars,
                            primary_tokens,
                            retained_tokens,
                        )
                        == expected
                    )

    for candidate_chars in (0, 31, 32, 100):
        for existing_chars in (0, 31, 32, 100):
            for shared_tokens, candidate_tokens, existing_tokens in (
                (0, 0, 0),
                (8, 10, 10),
                (9, 10, 10),
                (17, 20, 20),
            ):
                for similarity in (0, 879, 880, 1000, 1001):
                    token_duplicate = (
                        candidate_tokens > 0
                        and existing_tokens > 0
                        and shared_tokens * 1000 >= candidate_tokens * 850
                    )
                    expected = int(
                        candidate_chars >= 32
                        and existing_chars >= 32
                        and (min(similarity, 1000) >= 880 or token_duplicate)
                    )
                    assert (
                        library.ittm_should_drop_text_block(
                            candidate_chars,
                            existing_chars,
                            shared_tokens,
                            candidate_tokens,
                            existing_tokens,
                            similarity,
                        )
                        == expected
                    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Python/Rust pipeline core parity.")
    parser.add_argument("library", type=Path)
    args = parser.parse_args()
    verify(args.library.resolve())
    print("pipeline core Python/Rust parity: pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
