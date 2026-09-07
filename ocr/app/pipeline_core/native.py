from __future__ import annotations

import ctypes
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PIPELINE_CORE_LIBRARY_ENV = "ITTM_PIPELINE_CORE_LIB"
PIPELINE_CORE_ABI_VERSION = 6


@dataclass(frozen=True)
class NativePipelineCore:
    path: Path
    _library: ctypes.CDLL

    @classmethod
    def load(cls, path: Path) -> "NativePipelineCore":
        resolved = path.expanduser().resolve()
        library = ctypes.CDLL(str(resolved))
        try:
            abi_version = library.ittm_pipeline_abi_version
        except AttributeError as error:
            raise RuntimeError(f"Pipeline core has no ABI marker: {resolved}") from error
        abi_version.restype = ctypes.c_uint32
        version = int(abi_version())
        if version != PIPELINE_CORE_ABI_VERSION:
            raise RuntimeError(f"Unsupported pipeline core ABI {version}; expected {PIPELINE_CORE_ABI_VERSION}")
        required_exports = (
            "ittm_pipeline_route_id",
            "ittm_pipeline_recipe_mask",
            "ittm_sparse_add_signal",
            "ittm_is_isolated_heading",
            "ittm_span_evidence_score",
            "ittm_should_replace_primary",
            "ittm_should_drop_text_block",
            "ittm_assembler_begin",
            "ittm_assembler_add_layout",
            "ittm_assembler_add_segment",
            "ittm_assembler_render_length",
            "ittm_assembler_render_copy",
            "ittm_assembler_drop",
            "ittm_pdf_native_begin",
            "ittm_pdf_native_add_item",
            "ittm_pdf_native_build",
            "ittm_pdf_native_render_length",
            "ittm_pdf_native_render_copy",
            "ittm_pdf_native_drop",
            "ittm_separated_begin",
            "ittm_separated_plan_begin",
            "ittm_separated_import_blocks_begin",
            "ittm_separated_import_block",
            "ittm_separated_import_block_geometry",
            "ittm_separated_import_segments_begin",
            "ittm_separated_import_layout",
            "ittm_separated_layout_count",
            "ittm_separated_layout_field",
            "ittm_separated_import_segment",
            "ittm_separated_import_segments_finish",
            "ittm_separated_import_ocr_job",
            "ittm_separated_import_ocr_finish",
            "ittm_separated_run_get_segment",
            "ittm_separated_segment_count",
            "ittm_separated_segment_field",
            "ittm_separated_segment_source",
            "ittm_separated_segment_text_length",
            "ittm_separated_segment_text_copy",
            "ittm_separated_start_ocr",
            "ittm_separated_job_count",
            "ittm_separated_job_text_length",
            "ittm_separated_job_text_copy",
            "ittm_separated_job_field",
            "ittm_separated_job_segment_count",
            "ittm_separated_job_segment_field",
            "ittm_separated_object_count",
            "ittm_separated_object_field",
            "ittm_separated_object_segment",
            "ittm_separated_block_count",
            "ittm_separated_block_field",
            "ittm_separated_block_segment",
            "ittm_separated_block_raster_field",
            "ittm_separated_block_raster_length",
            "ittm_separated_block_raster_copy",
            "ittm_separated_topology_row_count",
            "ittm_separated_topology_row_field",
            "ittm_separated_topology_source_row",
            "ittm_separated_topology_slot_field",
            "ittm_separated_job_raster_field",
            "ittm_separated_job_raster_length",
            "ittm_separated_job_raster_copy",
            "ittm_separated_set_ocr",
            "ittm_separated_render_length",
            "ittm_separated_render_copy",
            "ittm_separated_stage_mask",
            "ittm_separated_drop",
        )
        missing_exports = [name for name in required_exports if not hasattr(library, name)]
        if missing_exports:
            raise RuntimeError(
                f"Pipeline core ABI {version} is incomplete at {resolved}: "
                + ", ".join(missing_exports)
            )
        library.ittm_pipeline_abi_version.restype = ctypes.c_uint32
        library.ittm_pipeline_route_id.restype = ctypes.c_uint32
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
        library.ittm_assembler_begin.argtypes = (ctypes.c_uint32,)
        library.ittm_assembler_begin.restype = ctypes.c_uint32
        library.ittm_assembler_add_layout.argtypes = (
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_assembler_add_layout.restype = ctypes.c_int32
        library.ittm_assembler_add_segment.argtypes = (
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        library.ittm_assembler_add_segment.restype = ctypes.c_int32
        library.ittm_assembler_render_length.argtypes = (ctypes.c_uint32,)
        library.ittm_assembler_render_length.restype = ctypes.c_uint32
        library.ittm_assembler_render_copy.argtypes = (
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        library.ittm_assembler_render_copy.restype = ctypes.c_int32
        library.ittm_assembler_drop.argtypes = (ctypes.c_uint32,)
        library.ittm_assembler_drop.restype = ctypes.c_int32
        library.ittm_pdf_native_begin.restype = ctypes.c_uint32
        library.ittm_pdf_native_add_item.argtypes = (
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_uint32,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
        )
        library.ittm_pdf_native_add_item.restype = ctypes.c_int32
        library.ittm_pdf_native_build.argtypes = (ctypes.c_uint32,)
        library.ittm_pdf_native_build.restype = ctypes.c_int32
        library.ittm_pdf_native_render_length.argtypes = (ctypes.c_uint32,)
        library.ittm_pdf_native_render_length.restype = ctypes.c_uint32
        library.ittm_pdf_native_render_copy.argtypes = (
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        library.ittm_pdf_native_render_copy.restype = ctypes.c_int32
        library.ittm_pdf_native_drop.argtypes = (ctypes.c_uint32,)
        library.ittm_pdf_native_drop.restype = ctypes.c_int32
        library.ittm_separated_begin.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_begin.restype = ctypes.c_uint32
        library.ittm_separated_plan_begin.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_plan_begin.restype = ctypes.c_uint32
        library.ittm_separated_import_blocks_begin.argtypes = ()
        library.ittm_separated_import_blocks_begin.restype = ctypes.c_uint32
        library.ittm_separated_import_block.argtypes = (
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_import_block.restype = ctypes.c_int32
        library.ittm_separated_import_block_geometry.argtypes = (
            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        )
        library.ittm_separated_import_block_geometry.restype = ctypes.c_int32
        library.ittm_separated_import_segments_begin.argtypes = ()
        library.ittm_separated_import_segments_begin.restype = ctypes.c_uint32
        library.ittm_separated_import_layout.argtypes = (ctypes.c_uint32,) * 5
        library.ittm_separated_import_layout.restype = ctypes.c_int32
        library.ittm_separated_layout_count.argtypes = (ctypes.c_uint32,)
        library.ittm_separated_layout_count.restype = ctypes.c_uint32
        library.ittm_separated_layout_field.argtypes = (ctypes.c_uint32,) * 3
        library.ittm_separated_layout_field.restype = ctypes.c_int32
        library.ittm_separated_import_segment.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        library.ittm_separated_import_segment.restype = ctypes.c_int32
        library.ittm_separated_import_segments_finish.argtypes = (ctypes.c_uint32,)
        library.ittm_separated_import_segments_finish.restype = ctypes.c_int32
        library.ittm_separated_import_ocr_job.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_import_ocr_job.restype = ctypes.c_int32
        library.ittm_separated_import_ocr_finish.argtypes = (ctypes.c_uint32,)
        library.ittm_separated_import_ocr_finish.restype = ctypes.c_int32
        library.ittm_separated_run_get_segment.argtypes = (ctypes.c_uint32,)
        library.ittm_separated_run_get_segment.restype = ctypes.c_int32
        library.ittm_separated_segment_count.argtypes = (ctypes.c_uint32,)
        library.ittm_separated_segment_count.restype = ctypes.c_uint32
        library.ittm_separated_segment_field.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_segment_field.restype = ctypes.c_int32
        library.ittm_separated_segment_source.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_segment_source.restype = ctypes.c_int32
        library.ittm_separated_segment_text_length.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_segment_text_length.restype = ctypes.c_uint32
        library.ittm_separated_segment_text_copy.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        library.ittm_separated_segment_text_copy.restype = ctypes.c_int32
        library.ittm_separated_start_ocr.argtypes = (ctypes.c_uint32,)
        library.ittm_separated_start_ocr.restype = ctypes.c_int32
        library.ittm_separated_job_count.argtypes = (ctypes.c_uint32,)
        library.ittm_separated_job_count.restype = ctypes.c_uint32
        library.ittm_separated_job_text_length.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_job_text_length.restype = ctypes.c_uint32
        library.ittm_separated_job_text_copy.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        library.ittm_separated_job_text_copy.restype = ctypes.c_int32
        library.ittm_separated_job_field.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_job_field.restype = ctypes.c_int32
        library.ittm_separated_job_segment_count.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_job_segment_count.restype = ctypes.c_uint32
        library.ittm_separated_job_segment_field.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_job_segment_field.restype = ctypes.c_int32
        library.ittm_separated_object_count.argtypes = (ctypes.c_uint32,)
        library.ittm_separated_object_count.restype = ctypes.c_uint32
        library.ittm_separated_object_field.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_object_field.restype = ctypes.c_int32
        library.ittm_separated_object_segment.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_object_segment.restype = ctypes.c_int32
        library.ittm_separated_block_count.argtypes = (ctypes.c_uint32,)
        library.ittm_separated_block_count.restype = ctypes.c_uint32
        library.ittm_separated_block_field.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_block_field.restype = ctypes.c_int32
        library.ittm_separated_block_segment.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_block_segment.restype = ctypes.c_int32
        library.ittm_separated_block_raster_field.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_block_raster_field.restype = ctypes.c_int32
        library.ittm_separated_block_raster_length.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_block_raster_length.restype = ctypes.c_uint32
        library.ittm_separated_block_raster_copy.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        library.ittm_separated_block_raster_copy.restype = ctypes.c_int32
        library.ittm_separated_topology_row_count.argtypes = (ctypes.c_uint32,)
        library.ittm_separated_topology_row_count.restype = ctypes.c_uint32
        library.ittm_separated_topology_row_field.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_topology_row_field.restype = ctypes.c_int32
        library.ittm_separated_topology_source_row.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_topology_source_row.restype = ctypes.c_int32
        library.ittm_separated_topology_slot_field.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_topology_slot_field.restype = ctypes.c_int32
        library.ittm_separated_job_raster_field.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_job_raster_field.restype = ctypes.c_int32
        library.ittm_separated_job_raster_length.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_job_raster_length.restype = ctypes.c_uint32
        library.ittm_separated_job_raster_copy.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        library.ittm_separated_job_raster_copy.restype = ctypes.c_int32
        library.ittm_separated_set_ocr.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_set_ocr.restype = ctypes.c_int32
        library.ittm_separated_add_ocr_word.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_add_ocr_word.restype = ctypes.c_int32
        library.ittm_separated_add_ocr_word_ppm.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        library.ittm_separated_add_ocr_word_ppm.restype = ctypes.c_int32
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
        return cls(path=resolved, _library=library)

    def route_id(self) -> int:
        return int(self._library.ittm_pipeline_route_id())

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

    def assemble_topology(
        self,
        source: int,
        layouts: list[tuple[str, int, int, int]],
        segments: list[tuple[str, str, int, int, int, int, int, str]],
    ) -> str:
        handle = int(self._library.ittm_assembler_begin(source))
        if handle == 0:
            raise ValueError(f"Topology assembler rejected source {source}")
        try:
            for object_id, object_kind, row_count, column_count in layouts:
                encoded_id = object_id.encode("utf-8")
                id_buffer = ctypes.create_string_buffer(encoded_id)
                status = int(
                    self._library.ittm_assembler_add_layout(
                        handle,
                        id_buffer,
                        len(encoded_id),
                        object_kind,
                        row_count,
                        column_count,
                    )
                )
                if status != 0:
                    raise ValueError(
                        f"Topology layout handoff failed with status {status}"
                    )
            for (
                segment_id,
                object_id,
                object_kind,
                row,
                column,
                row_span,
                column_span,
                text,
            ) in segments:
                encoded_segment_id = segment_id.encode("utf-8")
                encoded_object_id = object_id.encode("utf-8")
                encoded_text = text.encode("utf-8")
                segment_buffer = ctypes.create_string_buffer(encoded_segment_id)
                object_buffer = ctypes.create_string_buffer(encoded_object_id)
                text_buffer = (
                    ctypes.create_string_buffer(encoded_text) if encoded_text else None
                )
                status = int(
                    self._library.ittm_assembler_add_segment(
                        handle,
                        segment_buffer,
                        len(encoded_segment_id),
                        object_buffer,
                        len(encoded_object_id),
                        object_kind,
                        row,
                        column,
                        row_span,
                        column_span,
                        text_buffer,
                        len(encoded_text),
                    )
                )
                if status != 0:
                    raise ValueError(
                        f"Topology segment handoff failed with status {status}"
                    )
            length = int(self._library.ittm_assembler_render_length(handle))
            if length == 0:
                return ""
            output = ctypes.create_string_buffer(length)
            copied = int(
                self._library.ittm_assembler_render_copy(handle, output, length)
            )
            if copied != length:
                raise RuntimeError(
                    f"Topology assembler copied {copied} bytes; expected {length}"
                )
            return bytes(output.raw[:length]).decode("utf-8")
        finally:
            if int(self._library.ittm_assembler_drop(handle)) != 0:
                raise ValueError(f"Unknown topology assembler handle: {handle}")

    def build_native_pdf_parts(self, items: list[dict]) -> dict | None:
        handle = int(self._library.ittm_pdf_native_begin())
        if handle == 0:
            raise RuntimeError("Rust native PDF route rejected a new session")
        try:
            for item in items:
                encoded = str(item.get("text", "")).encode("utf-8")
                text_buffer = ctypes.create_string_buffer(encoded) if encoded else None
                transform = tuple(float(value) for value in item.get("transform", ()))
                if len(transform) != 6:
                    raise ValueError("Native PDF transform must contain six values")
                height = item.get("height")
                status = int(
                    self._library.ittm_pdf_native_add_item(
                        handle,
                        text_buffer,
                        len(encoded),
                        float(item.get("width", 0.0)),
                        float(height) if height is not None else 0.0,
                        int(height is not None),
                        *transform,
                    )
                )
                if status != 0:
                    raise RuntimeError(
                        f"Rust native PDF item handoff failed with status {status}"
                    )
            build_status = int(self._library.ittm_pdf_native_build(handle))
            if build_status != 0:
                raise RuntimeError(
                    f"Rust native PDF build failed with status {build_status}"
                )
            length = int(self._library.ittm_pdf_native_render_length(handle))
            if length <= 0:
                raise RuntimeError("Rust native PDF route returned no artifact")
            output = ctypes.create_string_buffer(length)
            copied = int(
                self._library.ittm_pdf_native_render_copy(handle, output, length)
            )
            if copied != length:
                raise RuntimeError(
                    f"Rust native PDF route copied {copied} bytes; expected {length}"
                )
            artifact = json.loads(bytes(output.raw[:length]).decode("utf-8"))
            if artifact is None:
                return None
            if not isinstance(artifact.get("objects"), list) or not isinstance(
                artifact.get("segments"), list
            ):
                raise RuntimeError("Rust native PDF route returned an invalid artifact")
            return artifact
        finally:
            if int(self._library.ittm_pdf_native_drop(handle)) != 0:
                raise ValueError(f"Unknown Rust native PDF handle: {handle}")

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

    def separated_plan_begin(
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
            self._library.ittm_separated_plan_begin(
                source,
                len(pixels),
                width,
                height,
                stride,
                pixel_format,
            )
        )
        if handle == 0:
            raise ValueError("Separated planning stage rejected the raster plane")
        return handle

    def separated_start_ocr(self, handle: int) -> None:
        if int(self._library.ittm_separated_start_ocr(handle)) != 1:
            raise ValueError("Separated OCR stage could not start")

    def separated_import_blocks_begin(self) -> int:
        handle = int(self._library.ittm_separated_import_blocks_begin())
        if handle == 0:
            raise RuntimeError("Separated block import could not start")
        return handle

    def separated_import_segments_begin(self) -> int:
        handle = int(self._library.ittm_separated_import_segments_begin())
        if handle == 0:
            raise RuntimeError("Separated segment import could not start")
        return handle

    def separated_import_segment(
        self,
        handle: int,
        object_id: int,
        object_kind: int,
        cell: tuple[int, int, int, int],
        source_indexes: tuple[int, ...],
        text: str,
    ) -> None:
        sources = (
            (ctypes.c_uint32 * len(source_indexes))(*source_indexes)
            if source_indexes
            else None
        )
        encoded = text.encode("utf-8")
        text_buffer = ctypes.create_string_buffer(encoded) if encoded else None
        result = int(
            self._library.ittm_separated_import_segment(
                handle,
                object_id,
                object_kind,
                *cell,
                sources,
                len(source_indexes),
                text_buffer,
                len(encoded),
            )
        )
        if result != 0:
            raise ValueError(f"Separated segment import failed: {result}")

    def separated_import_layout(
        self, handle: int, object_id: int, object_kind: int,
        rows: int, columns: int,
    ) -> None:
        status = int(self._library.ittm_separated_import_layout(
            handle, object_id, object_kind, rows, columns,
        ))
        if status != 0:
            raise ValueError(f"Separated layout import failed: {status}")

    def separated_layouts(self, handle: int) -> tuple[tuple[int, int, int, int], ...]:
        return tuple(
            tuple(int(self._library.ittm_separated_layout_field(handle, index, field))
                  for field in range(4))
            for index in range(int(self._library.ittm_separated_layout_count(handle)))
        )

    def separated_import_segments_finish(self, handle: int) -> None:
        if int(self._library.ittm_separated_import_segments_finish(handle)) != 1:
            raise ValueError("Separated segment import could not finish")

    def separated_import_block(
        self,
        handle: int,
        metadata: tuple[int, ...],
        pixels: bytes,
        width: int,
        height: int,
        stride: int,
    ) -> None:
        if not metadata or not pixels:
            raise ValueError("Imported separated block must not be empty")
        metadata_buffer = (ctypes.c_uint32 * len(metadata))(*metadata)
        pixel_buffer = ctypes.create_string_buffer(pixels)
        result = int(
            self._library.ittm_separated_import_block(
                handle,
                metadata_buffer,
                len(metadata),
                pixel_buffer,
                len(pixels),
                width,
                height,
                stride,
            )
        )
        if result != 0:
            raise ValueError(f"Separated block import failed: {result}")

    def separated_import_block_geometry(
        self, handle: int, metadata: tuple[int, ...], width: int, height: int, stride: int,
    ) -> None:
        if not metadata:
            raise ValueError("Imported block geometry must not be empty")
        buffer = (ctypes.c_uint32 * len(metadata))(*metadata)
        status = int(self._library.ittm_separated_import_block_geometry(
            handle, buffer, len(metadata), width, height, stride,
        ))
        if status != 0:
            raise ValueError(f"Separated block geometry import failed: {status}")

    def separated_import_ocr_job(
        self,
        handle: int,
        block_index: int,
        profile: int,
        transform: int,
        words_in_source_space: bool,
        text: str,
        grammar_milli: int,
    ) -> int:
        encoded = text.encode("utf-8")
        source = ctypes.create_string_buffer(encoded) if encoded else None
        index = int(
            self._library.ittm_separated_import_ocr_job(
                handle,
                block_index,
                profile,
                transform,
                int(words_in_source_space),
                source,
                len(encoded),
                grammar_milli,
            )
        )
        if index < 0:
            raise ValueError(f"Separated OCR import failed: {index}")
        return index

    def separated_import_ocr_finish(self, handle: int) -> None:
        if int(self._library.ittm_separated_import_ocr_finish(handle)) != 1:
            raise ValueError("Separated OCR import could not finish")

    def separated_run_get_segment(self, handle: int) -> None:
        if int(self._library.ittm_separated_run_get_segment(handle)) != 1:
            raise ValueError("Separated get-segment stage could not run")

    def separated_segment_count(self, handle: int) -> int:
        return int(self._library.ittm_separated_segment_count(handle))

    def separated_segment_field(self, handle: int, index: int, field: int) -> int:
        value = int(self._library.ittm_separated_segment_field(handle, index, field))
        if value < 0:
            raise ValueError(f"Invalid separated segment field: {index}:{field}")
        return value

    def separated_segment_source(self, handle: int, index: int, source: int) -> int:
        value = int(self._library.ittm_separated_segment_source(handle, index, source))
        if value < 0:
            raise ValueError(f"Invalid separated segment source: {index}:{source}")
        return value

    def separated_segment_text(self, handle: int, index: int) -> str:
        length = int(self._library.ittm_separated_segment_text_length(handle, index))
        if length == 0:
            return ""
        output = ctypes.create_string_buffer(length)
        result = int(
            self._library.ittm_separated_segment_text_copy(
                handle,
                index,
                output,
                length,
            )
        )
        if result != 0:
            raise ValueError(f"Invalid separated segment text: {index}:{result}")
        return output.raw.decode("utf-8")

    def separated_job_count(self, handle: int) -> int:
        return int(self._library.ittm_separated_job_count(handle))

    def separated_job_text(self, handle: int, index: int) -> str:
        length = int(self._library.ittm_separated_job_text_length(handle, index))
        if length == 0:
            return ""
        output = ctypes.create_string_buffer(length)
        result = int(
            self._library.ittm_separated_job_text_copy(
                handle,
                index,
                output,
                length,
            )
        )
        if result != 0:
            raise ValueError(f"Invalid separated OCR job text: {index}:{result}")
        return output.raw.decode("utf-8")

    def separated_job_field(self, handle: int, index: int, field: int) -> int:
        value = int(self._library.ittm_separated_job_field(handle, index, field))
        if value < 0:
            raise ValueError(f"Invalid separated OCR job field: {index}:{field}")
        return value

    def separated_job_segment_count(self, handle: int, index: int) -> int:
        return int(self._library.ittm_separated_job_segment_count(handle, index))

    def separated_job_segment_field(
        self,
        handle: int,
        index: int,
        segment_index: int,
        field: int,
    ) -> int:
        return int(
            self._library.ittm_separated_job_segment_field(
                handle,
                index,
                segment_index,
                field,
            )
        )

    def separated_object_count(self, handle: int) -> int:
        return int(self._library.ittm_separated_object_count(handle))

    def separated_object_field(self, handle: int, index: int, field: int) -> int:
        value = int(self._library.ittm_separated_object_field(handle, index, field))
        if value < 0:
            raise ValueError(f"Invalid separated object field: {index}:{field}")
        return value

    def separated_object_segment(self, handle: int, object_index: int, index: int) -> int:
        value = int(
            self._library.ittm_separated_object_segment(handle, object_index, index)
        )
        if value < 0:
            raise ValueError(f"Invalid separated object segment: {object_index}:{index}")
        return value

    def separated_block_count(self, handle: int) -> int:
        return int(self._library.ittm_separated_block_count(handle))

    def separated_block_field(self, handle: int, index: int, field: int) -> int:
        value = int(self._library.ittm_separated_block_field(handle, index, field))
        if value < 0:
            raise ValueError(f"Invalid separated block field: {index}:{field}")
        return value

    def separated_block_segment(self, handle: int, block_index: int, index: int) -> int:
        value = int(self._library.ittm_separated_block_segment(handle, block_index, index))
        if value < 0:
            raise ValueError(f"Invalid separated block segment: {block_index}:{index}")
        return value

    def separated_block_raster(
        self,
        handle: int,
        index: int,
    ) -> tuple[bytes, int, int, int, int]:
        fields = tuple(
            int(self._library.ittm_separated_block_raster_field(handle, index, field))
            for field in range(4)
        )
        if any(value <= 0 for value in fields):
            raise ValueError(f"Invalid separated block raster fields: {index}:{fields}")
        width, height, stride, pixel_format = fields
        length = int(self._library.ittm_separated_block_raster_length(handle, index))
        if length != stride * height:
            raise RuntimeError(
                f"Separated block raster length {length} disagrees with {stride}x{height}"
            )
        output = ctypes.create_string_buffer(length)
        copied = int(
            self._library.ittm_separated_block_raster_copy(
                handle,
                index,
                output,
                length,
            )
        )
        if copied != length:
            raise RuntimeError(
                f"Separated block raster copied {copied} bytes; expected {length}"
            )
        return bytes(output.raw[:length]), width, height, stride, pixel_format

    def separated_topology_row_count(self, handle: int) -> int:
        return int(self._library.ittm_separated_topology_row_count(handle))

    def separated_topology_row_field(self, handle: int, index: int, field: int) -> int:
        value = int(self._library.ittm_separated_topology_row_field(handle, index, field))
        if value < 0:
            raise ValueError(f"Invalid separated topology row field: {index}:{field}")
        return value

    def separated_topology_source_row(self, handle: int, row: int, index: int) -> int:
        value = int(self._library.ittm_separated_topology_source_row(handle, row, index))
        if value < 0:
            raise ValueError(f"Invalid separated topology source row: {row}:{index}")
        return value

    def separated_topology_slot_field(
        self,
        handle: int,
        row: int,
        slot: int,
        field: int,
    ) -> int:
        value = int(
            self._library.ittm_separated_topology_slot_field(handle, row, slot, field)
        )
        if value < 0:
            raise ValueError(f"Invalid separated topology slot field: {row}:{slot}:{field}")
        return value

    def separated_job_raster(
        self,
        handle: int,
        index: int,
    ) -> tuple[bytes, int, int, int, int]:
        fields = tuple(
            int(self._library.ittm_separated_job_raster_field(handle, index, field))
            for field in range(4)
        )
        if any(value <= 0 for value in fields):
            raise ValueError(f"Invalid separated OCR raster fields: {index}:{fields}")
        width, height, stride, pixel_format = fields
        length = int(self._library.ittm_separated_job_raster_length(handle, index))
        if length != stride * height:
            raise RuntimeError(
                f"Separated OCR raster length {length} disagrees with {stride}x{height}"
            )
        output = ctypes.create_string_buffer(length)
        copied = int(
            self._library.ittm_separated_job_raster_copy(
                handle,
                index,
                output,
                length,
            )
        )
        if copied != length:
            raise RuntimeError(
                f"Separated OCR raster copied {copied} bytes; expected {length}"
            )
        return bytes(output.raw[:length]), width, height, stride, pixel_format

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

    def separated_add_ocr_word(
        self,
        handle: int,
        index: int,
        text: str,
        bbox: tuple[int, int, int, int],
        confidence_milli: int,
    ) -> None:
        encoded = text.encode("utf-8")
        source = ctypes.create_string_buffer(encoded)
        status = int(
            self._library.ittm_separated_add_ocr_word_ppm(
                handle,
                index,
                source,
                len(encoded),
                *bbox,
                confidence_milli,
            )
        )
        if status != 0:
            raise ValueError(f"Separated OCR word handoff failed with status {status}")

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
        repo_root / "pipeline-core" / "target" / "release" / "libittm_pipeline_core.so",
        repo_root / "pipeline-core" / "target" / "debug" / "libittm_pipeline_core.so",
        Path(__file__).with_name("libittm_pipeline_core.so"),
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
