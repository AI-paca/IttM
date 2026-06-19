from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Protocol

from app.pipeline_config import OCR_PIPELINE_PROFILES, OcrPipelineProfile

PIPELINE_FLAG_OVERRIDES_ENV = "OCR_PIPELINE_FLAG_OVERRIDES"


@dataclass(frozen=True)
class PipelineFlag:
    key: str
    value: str
    source: str

    @property
    def serialized(self) -> str:
        if ":" in self.key:
            return f"{self.key}={self.value}"
        return f"{self.key}:{self.value}"


class PipelineFlagProvider(Protocol):
    def pipeline_flags(self) -> Iterable[PipelineFlag]: ...


def flag_overrides_enabled() -> bool:
    return os.environ.get(PIPELINE_FLAG_OVERRIDES_ENV, "").casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def ensure_flag_overrides_allowed(raw_flags: str | None) -> None:
    if not raw_flags:
        return
    if flag_overrides_enabled():
        raise ValueError("Pipeline flag overrides are not implemented yet.")
    raise ValueError(
        "Pipeline flag overrides are exposed in the API contract but disabled. "
        f"Set {PIPELINE_FLAG_OVERRIDES_ENV}=1 only after an override resolver is implemented."
    )


def _flag(key: str, value, source: str) -> PipelineFlag:
    return PipelineFlag(key=key, value=str(value), source=source)


def profile_flag_items(profile: OcrPipelineProfile) -> list[PipelineFlag]:
    flags: list[PipelineFlag] = []
    flags.extend(
        _flag("preprocess", step, "OcrPipelineProfile.image_preprocessing") for step in profile.image_preprocessing
    )
    flags.extend(
        [
            _flag(
                "ocr_language_priority",
                "+".join(profile.tesseract_language_priority),
                "OcrPipelineProfile.tesseract_language_priority",
            ),
            _flag(
                "ocr_text_region_psm",
                profile.text_region_psm,
                "OcrPipelineProfile.text_region_psm",
            ),
            _flag(
                "ocr_document_region_psm",
                profile.document_region_psm,
                "OcrPipelineProfile.document_region_psm",
            ),
            _flag(
                "ocr_wide_text_region_psm",
                profile.wide_text_region_psm,
                "OcrPipelineProfile.wide_text_region_psm",
            ),
            _flag(
                "ocr_table_word_psm",
                profile.table_word_psm,
                "OcrPipelineProfile.table_word_psm",
            ),
            _flag(
                "ocr_large_table_word_psm",
                profile.large_table_word_psm,
                "OcrPipelineProfile.large_table_word_psm",
            ),
            _flag(
                "table_raw_text_fallback",
                profile.table_raw_text_fallback,
                "OcrPipelineProfile.table_raw_text_fallback",
            ),
            _flag(
                "table_raw_text_fallback_psm",
                profile.table_raw_text_fallback_psm,
                "OcrPipelineProfile.table_raw_text_fallback_psm",
            ),
            _flag(
                "table_raw_text_fallback_min_rows",
                profile.table_raw_text_fallback_min_rows,
                "OcrPipelineProfile.table_raw_text_fallback_min_rows",
            ),
            _flag(
                "table_raw_text_fallback_min_cols",
                profile.table_raw_text_fallback_min_cols,
                "OcrPipelineProfile.table_raw_text_fallback_min_cols",
            ),
            _flag(
                "table_raw_text_fallback_max_cols",
                profile.table_raw_text_fallback_max_cols,
                "OcrPipelineProfile.table_raw_text_fallback_max_cols",
            ),
            _flag(
                "table_raw_text_fallback_min_ratio",
                profile.table_raw_text_fallback_min_ratio,
                "OcrPipelineProfile.table_raw_text_fallback_min_ratio",
            ),
            _flag(
                "sparse_text_fallback_engine",
                profile.sparse_text_fallback_engine or "none",
                "OcrPipelineProfile.sparse_text_fallback_engine",
            ),
            _flag(
                "sparse_text_fallback_min_tokens",
                profile.sparse_text_fallback_min_tokens,
                "OcrPipelineProfile.sparse_text_fallback_min_tokens",
            ),
            _flag(
                "sparse_text_fallback_min_ratio",
                profile.sparse_text_fallback_min_ratio,
                "OcrPipelineProfile.sparse_text_fallback_min_ratio",
            ),
            _flag(
                "dense_grid_fallback",
                profile.dense_grid_fallback,
                "OcrPipelineProfile.dense_grid_fallback",
            ),
            _flag(
                "dense_grid_target_width",
                profile.dense_grid_target_width,
                "OcrPipelineProfile.dense_grid_target_width",
            ),
            _flag(
                "ocr_border_pixels",
                profile.ocr_border_pixels,
                "OcrPipelineProfile.ocr_border_pixels",
            ),
            _flag(
                "edge_word_fallback_min_tokens",
                profile.edge_word_fallback_min_tokens,
                "OcrPipelineProfile.edge_word_fallback_min_tokens",
            ),
            _flag(
                "layout_selector",
                profile.layout.selector,
                "LayoutPipelineConfig.selector",
            ),
            _flag(
                "grid_min_confirmed_cell_ratio",
                profile.grid_min_confirmed_cell_ratio,
                "OcrPipelineProfile.grid_min_confirmed_cell_ratio",
            ),
            _flag(
                "table_min_word_cell_coverage",
                profile.table_min_word_cell_coverage,
                "OcrPipelineProfile.table_min_word_cell_coverage",
            ),
            _flag(
                "wide_table_min_word_cell_coverage",
                profile.wide_table_min_word_cell_coverage,
                "OcrPipelineProfile.wide_table_min_word_cell_coverage",
            ),
            _flag(
                "table_min_cell_coverage",
                profile.table_min_cell_coverage,
                "OcrPipelineProfile.table_min_cell_coverage",
            ),
            _flag(
                "max_table_cell_ocr_calls",
                profile.max_table_cell_ocr_calls,
                "OcrPipelineProfile.max_table_cell_ocr_calls",
            ),
            _flag(
                "table_layout_normalization",
                profile.table_layout_normalization,
                "OcrPipelineProfile.table_layout_normalization",
            ),
            _flag(
                "table_word_recognition",
                profile.table_word_recognition,
                "OcrPipelineProfile.table_word_recognition",
            ),
        ]
    )
    flags.extend(
        _flag("layout_stage", stage, "LayoutPipelineConfig.allowed_stages") for stage in profile.layout.allowed_stages
    )
    flags.extend(
        _flag(
            "edge_word_fallback_psm",
            psm,
            "OcrPipelineProfile.edge_word_fallback_psms",
        )
        for psm in profile.edge_word_fallback_psms
    )
    flags.extend(
        _flag(f"layout_param:{name}", value, "LayoutPipelineConfig.default_parameters")
        for name, value in profile.layout.default_parameters
    )
    flags.extend(
        _flag("table_word_formatter", name, "OcrPipelineProfile.table_word_formatters")
        for name in profile.table_word_formatters
    )
    return flags


def profile_flags(profile: OcrPipelineProfile) -> set[str]:
    return {flag.serialized for flag in profile_flag_items(profile)}


def profile_flags_string(profile: OcrPipelineProfile) -> str:
    return "; ".join(sorted(profile_flags(profile)))


def pipeline_flag_catalog() -> list[dict[str, str]]:
    by_key: dict[str, PipelineFlag] = {}
    for profile in OCR_PIPELINE_PROFILES.values():
        for flag in profile_flag_items(profile):
            by_key.setdefault(flag.key, flag)
    by_key.setdefault("pipeline_flags", _flag("pipeline_flags", "disabled", "API query contract"))
    by_key.setdefault(
        "overrides_enabled",
        _flag("overrides_enabled", flag_overrides_enabled(), "API query contract"),
    )
    by_key.setdefault(
        "ocr_runtime",
        _flag("ocr_runtime", "tesseract.js|backend", "browser/debug runner"),
    )
    by_key.setdefault(
        "ocr_languages",
        _flag("ocr_languages", "rus+eng+chi_sim", "browser/debug runner"),
    )
    by_key.setdefault("ocr_max_dimension", _flag("ocr_max_dimension", "3200", "browser profile"))
    by_key.setdefault(
        "ocr_max_image_pixels",
        _flag("ocr_max_image_pixels", "8000000", "browser profile"),
    )
    by_key.setdefault(
        "browser_cache_worker",
        _flag("browser_cache_worker", "false", "browser profile"),
    )
    by_key.setdefault(
        "browser_profile_reason",
        _flag("browser_profile_reason", "balanced-browser-fallback", "browser profile"),
    )
    by_key.setdefault("pdf_render_scale", _flag("pdf_render_scale", "1.25", "browser profile"))
    by_key.setdefault(
        "pdf_mode",
        _flag("pdf_mode", "auto|raster", "API query contract"),
    )
    by_key.setdefault(
        "preprocess_runtime",
        _flag("preprocess_runtime", "browser_canvas|python_compat|none", "debug runner"),
    )
    return [
        {
            "key": key,
            "example": flag.serialized,
            "source": flag.source,
        }
        for key, flag in sorted(by_key.items())
    ]


def pipeline_flags_payload() -> dict:
    return {
        "overrides_enabled": flag_overrides_enabled(),
        "override_parameter": "pipeline_flags",
        "available_flags": pipeline_flag_catalog(),
        "profiles": {name: sorted(profile_flags(profile)) for name, profile in sorted(OCR_PIPELINE_PROFILES.items())},
    }
