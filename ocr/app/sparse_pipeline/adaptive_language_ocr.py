from __future__ import annotations

import concurrent.futures
import csv
import hashlib
import io
import math
import re
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from app.sparse_pipeline.block_crops import BlockCropPair
from app.sparse_pipeline.block_planning import BlockPlan
from app.sparse_pipeline.contracts import Box, SegmentSpan
from app.sparse_pipeline.crop_enhancement import (
    CropInput,
    GammaDarkCropEnhancer,
)
from app.sparse_pipeline.ocr_adapter_contracts import (
    OcrFailureCode,
    OcrOutputGeometry,
    OcrResource,
)
from app.sparse_pipeline.ocr_queue import (
    OcrEngineOutput,
    OcrJobResult,
    OcrJobStatus,
    OcrLane,
    OcrQueueResult,
    OcrQueueStatus,
    OcrTransform,
    OcrWord,
)


@dataclass(frozen=True)
class CompactionPlacement:
    unit_id: str
    segment_ids: tuple[str, ...]
    source_bbox: Box
    crop_bbox: Box


@dataclass(frozen=True)
class BlockCompaction:
    block_id: str
    placements: tuple[CompactionPlacement, ...]
    omitted_empty_units: tuple[str, ...]
    occupied_pixels_before: int
    packed_canvas_pixels: int


@dataclass(frozen=True)
class LanguageProfile:
    profile_id: str
    languages: tuple[str, ...]
    resource: OcrResource
    worker_factory: Callable[[], object]


@dataclass
class SplayLanguageNode:
    profile_id: str
    languages: tuple[str, ...]
    initial_rank: int = 0
    quality_sum: float = 0.0
    attempts: int = 0
    exact_grammar: int = 0
    garbage: int = 0
    last_grammar_percent: int = 0

    @property
    def weight(self) -> float:
        return (1.0 + self.quality_sum) / (2.0 + self.attempts)


@dataclass(frozen=True)
class GrammarAssessment:
    percent: int
    exact: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class LanguageAttempt:
    sequence: int
    block_id: str
    unit_id: str
    profile_id: str
    languages: tuple[str, ...]
    transform: OcrTransform
    status: str
    grammar_percent: int
    mean_confidence: float
    text: str
    error: str
    elapsed_seconds: float


class LanguageSplayState:
    """Typed adaptive state; CSV is only a human-readable projection."""

    def __init__(self, profiles: tuple[LanguageProfile, ...]) -> None:
        self.nodes = [
            SplayLanguageNode(
                profile.profile_id,
                profile.languages,
                initial_rank=rank,
            )
            for rank, profile in enumerate(profiles)
        ]
        self.attempts: list[LanguageAttempt] = []
        self.locked_profile_id: str | None = None
        self._sequence = 0

    def ensure_profiles(self, profiles: tuple[LanguageProfile, ...]) -> None:
        existing = {node.profile_id for node in self.nodes}
        for profile in profiles:
            if profile.profile_id not in existing:
                self.nodes.append(
                    SplayLanguageNode(
                        profile.profile_id,
                        profile.languages,
                        initial_rank=len(self.nodes),
                    )
                )
                existing.add(profile.profile_id)

    def ordered_profile_ids(self) -> tuple[str, ...]:
        return tuple(node.profile_id for node in self.nodes)

    def observe(self, profile_id: str, grammar_percent: int) -> None:
        node = next(
            node for node in self.nodes if node.profile_id == profile_id
        )
        node.attempts += 1
        node.quality_sum += grammar_percent / 100.0
        node.last_grammar_percent = grammar_percent
        if grammar_percent == 100:
            node.exact_grammar += 1
        elif grammar_percent < 25:
            node.garbage += 1
        self.nodes.sort(
            key=lambda item: (
                -item.exact_grammar,
                -(item.exact_grammar / max(1, item.attempts)),
                item.garbage,
                -item.weight,
                item.initial_rank,
            )
        )

    def record(
        self,
        *,
        block_id: str,
        unit_id: str,
        profile: LanguageProfile,
        transform: OcrTransform,
        status: str,
        assessment: GrammarAssessment,
        mean_confidence: float,
        text: str,
        error: str,
        elapsed_seconds: float,
    ) -> None:
        self._sequence += 1
        self.attempts.append(
            LanguageAttempt(
                sequence=self._sequence,
                block_id=block_id,
                unit_id=unit_id,
                profile_id=profile.profile_id,
                languages=profile.languages,
                transform=transform,
                status=status,
                grammar_percent=assessment.percent,
                mean_confidence=mean_confidence,
                text=text,
                error=error,
                elapsed_seconds=elapsed_seconds,
            )
        )

    def merge_attempts(
        self,
        attempts: tuple[LanguageAttempt, ...],
    ) -> None:
        for attempt in attempts:
            self._sequence += 1
            self.attempts.append(
                replace(attempt, sequence=self._sequence)
            )

    def write_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        total_weight = sum(node.weight for node in self.nodes) or 1.0
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                (
                    "rank",
                    "profile",
                    "languages",
                    "locked",
                    "probability",
                    "attempts",
                    "exact_grammar",
                    "garbage",
                    "last_grammar_percent",
                )
            )
            for rank, node in enumerate(self.nodes, start=1):
                writer.writerow(
                    (
                        rank,
                        node.profile_id,
                        "+".join(node.languages),
                        "yes"
                        if node.profile_id == self.locked_profile_id
                        else "no",
                        f"{node.weight / total_weight:.6f}",
                        node.attempts,
                        node.exact_grammar,
                        node.garbage,
                        node.last_grammar_percent,
                    )
                )
        attempts_path = path.with_name(f"{path.stem}-attempts.csv")
        with attempts_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                (
                    "sequence",
                    "block_id",
                    "unit_id",
                    "profile",
                    "languages",
                    "transform",
                    "status",
                    "grammar_percent",
                    "mean_confidence",
                    "text",
                    "error",
                    "elapsed_seconds",
                )
            )
            for attempt in self.attempts:
                writer.writerow(
                    (
                        attempt.sequence,
                        attempt.block_id,
                        attempt.unit_id,
                        attempt.profile_id,
                        "+".join(attempt.languages),
                        attempt.transform.value,
                        attempt.status,
                        attempt.grammar_percent,
                        f"{attempt.mean_confidence:.6f}",
                        attempt.text,
                        attempt.error,
                        f"{attempt.elapsed_seconds:.6f}",
                    )
                )


@dataclass(frozen=True)
class _Tile:
    unit_id: str
    segment_ids: tuple[str, ...]
    source_bbox: Box
    pixels: np.ndarray


def _png_bytes(pixels: np.ndarray) -> bytes:
    output = io.BytesIO()
    image = Image.fromarray(pixels.astype(np.uint8, copy=False), mode="RGB")
    try:
        image.save(output, format="PNG", compress_level=1, dpi=(300, 300))
    finally:
        image.close()
    return output.getvalue()


def _pack_tiles(
    tiles: tuple[_Tile, ...],
) -> tuple[
    bytes,
    tuple[CompactionPlacement, ...],
    int,
    tuple[int, int],
]:
    gap = 8
    total_tile_pixels = sum(
        tile.pixels.shape[0] * tile.pixels.shape[1] for tile in tiles
    )
    target_width = max(
        max(tile.pixels.shape[1] for tile in tiles),
        math.ceil(math.sqrt(total_tile_pixels)),
    )
    mutable_rows: list[list[_Tile]] = []
    current_row: list[_Tile] = []
    current_width = 0
    for tile in tiles:
        tile_width = tile.pixels.shape[1]
        projected_width = (
            current_width + gap + tile_width
            if current_row
            else tile_width
        )
        if current_row and projected_width > target_width:
            mutable_rows.append(current_row)
            current_row = []
            current_width = 0
        current_row.append(tile)
        current_width = (
            current_width + gap + tile_width
            if len(current_row) > 1
            else tile_width
        )
    if current_row:
        mutable_rows.append(current_row)
    rows = tuple(tuple(row) for row in mutable_rows)
    row_heights = tuple(
        max(tile.pixels.shape[0] for tile in row) for row in rows
    )
    row_tops = []
    top = 0
    for height in row_heights:
        row_tops.append(top)
        top += height + gap
    row_widths = tuple(
        sum(tile.pixels.shape[1] for tile in row) + gap * (len(row) - 1)
        for row in rows
    )
    atlas_width = max(row_widths)
    atlas_height = sum(row_heights) + gap * (len(rows) - 1)
    source_placements_list: list[tuple[_Tile, int, int]] = []
    for row, row_top in zip(rows, row_tops):
        left = 0
        for tile in row:
            source_placements_list.append((tile, left, row_top))
            left += tile.pixels.shape[1] + gap
    source_placements = tuple(source_placements_list)
    atlas = np.full((atlas_height, atlas_width, 3), 255, dtype=np.uint8)
    for tile, left, top in source_placements:
        height, width = tile.pixels.shape[:2]
        atlas[top : top + height, left : left + width] = tile.pixels

    text_line_heights: list[int] = []
    for tile in tiles:
        ink = np.any(tile.pixels < 248, axis=2)
        height = ink.shape[0]
        non_rule_columns = np.count_nonzero(ink, axis=0) < max(
            2,
            math.ceil(height * 0.75),
        )
        if np.any(non_rule_columns):
            ink = ink[:, non_rule_columns]
        occupied_rows = np.flatnonzero(np.any(ink, axis=1))
        if not occupied_rows.size:
            continue
        run_start = int(occupied_rows[0])
        previous = run_start
        for row in map(int, occupied_rows[1:]):
            if row > previous + 1:
                run_height = previous - run_start + 1
                if run_height >= 2:
                    text_line_heights.append(run_height)
                run_start = row
            previous = row
        run_height = previous - run_start + 1
        if run_height >= 2:
            text_line_heights.append(run_height)
    median_text_height = (
        sorted(text_line_heights)[len(text_line_heights) // 2]
        if text_line_heights
        else 24
    )
    requested_scale = min(
        4,
        max(1, math.ceil(24 / max(1, median_text_height))),
    )
    pixel_limited_scale = max(
        1,
        math.floor(
            math.sqrt(
                16_000_000 / max(1, atlas_width * atlas_height)
            )
        ),
    )
    scale = min(requested_scale, pixel_limited_scale)
    if scale > 1:
        atlas_image = Image.fromarray(atlas, mode="RGB")
        try:
            resized = atlas_image.resize(
                (atlas_width * scale, atlas_height * scale),
                Image.Resampling.LANCZOS,
            )
            try:
                payload = _png_bytes(np.asarray(resized).copy())
            finally:
                resized.close()
        finally:
            atlas_image.close()
    else:
        payload = _png_bytes(atlas)

    placements = []
    for tile, left, top in source_placements:
        tile_height, tile_width = tile.pixels.shape[:2]
        placements.append(
            CompactionPlacement(
                unit_id=tile.unit_id,
                segment_ids=tile.segment_ids,
                source_bbox=tile.source_bbox,
                crop_bbox=Box(
                    left * scale,
                    top * scale,
                    (left + tile_width) * scale,
                    (top + tile_height) * scale,
                ),
            )
        )
    return (
        payload,
        tuple(placements),
        atlas_width * atlas_height * scale * scale,
        (atlas_width * scale, atlas_height * scale),
    )


def build_deferred_compact_crops(
    page_png: bytes,
    *,
    aligned_size: tuple[int, int],
    plan: BlockPlan,
    ownership: np.ndarray | None,
    ownership_segment_ids: tuple[str, ...] | None,
    segment_spans: tuple[SegmentSpan, ...] = (),
) -> tuple[tuple[BlockCropPair, ...], tuple[BlockCompaction, ...]]:
    """Build raw compact crops; enhancement is deferred to each OCR attempt."""

    with Image.open(io.BytesIO(page_png)) as opened:
        opened.load()
        if opened.format != "PNG" or opened.size != aligned_size:
            raise ValueError("deferred crop source disagrees with aligned geometry")
        page = np.asarray(opened.convert("RGB")).copy()
    label_by_segment = {
        segment_id: label
        for label, segment_id in enumerate(ownership_segment_ids or ())
    }
    span_by_segment = {
        span.segment_id: span for span in segment_spans
    }
    outputs: list[BlockCropPair] = []
    compacted: list[BlockCompaction] = []
    for block in plan.blocks:
        bbox = block.bbox
        crop_bbox = bbox
        pixels = page[bbox.top : bbox.bottom, bbox.left : bbox.right].copy()
        region_ownership = (
            ownership[bbox.top : bbox.bottom, bbox.left : bbox.right]
            if ownership is not None
            else None
        )
        selected_labels = {
            label_by_segment[segment_id]
            for segment_id in block.segment_ids
            if segment_id in label_by_segment
        }
        masked_segment_ids: tuple[str, ...] = ()
        if region_ownership is not None:
            foreign_labels = set(
                int(value)
                for value in np.unique(region_ownership)
                if int(value) >= 0 and int(value) not in selected_labels
            )
            if foreign_labels:
                pixels[np.isin(region_ownership, tuple(foreign_labels))] = 255
                masked_segment_ids = tuple(
                    segment_id
                    for segment_id, label in label_by_segment.items()
                    if label in foreign_labels
                )

        tiles: list[_Tile] = []
        omitted: list[str] = []
        if region_ownership is not None:
            unit_by_segment = {
                segment_id: unit
                for unit in plan.membership_units
                for segment_id in unit.segment_ids
            }
            grouped_segment_ids: dict[
                tuple[int, int, int, int, str],
                list[str],
            ] = {}
            for segment_id in block.segment_ids:
                span = span_by_segment.get(segment_id)
                key = (
                    (
                        span.row_start,
                        span.row_stop,
                        span.column_start,
                        span.column_stop,
                        "",
                    )
                    if span is not None
                    else (0, 0, 0, 0, segment_id)
                )
                grouped_segment_ids.setdefault(key, []).append(segment_id)
            for group_ids_list in grouped_segment_ids.values():
                group_ids = tuple(group_ids_list)
                unit = unit_by_segment.get(group_ids[0])
                labels = tuple(
                    label_by_segment[segment_id]
                    for segment_id in group_ids
                    if segment_id in label_by_segment
                )
                if unit is None or not labels:
                    if unit is not None:
                        omitted.append(unit.unit_id)
                    continue
                unit_mask = np.isin(region_ownership, labels)
                ink_mask = unit_mask & np.any(pixels < 248, axis=2)
                rows, columns = np.nonzero(ink_mask)
                if not len(rows):
                    omitted.append(unit.unit_id)
                    continue
                local_left = max(0, int(columns.min()) - 2)
                local_top = max(0, int(rows.min()) - 2)
                local_right = min(pixels.shape[1], int(columns.max()) + 3)
                local_bottom = min(pixels.shape[0], int(rows.max()) + 3)
                source_bbox = Box(
                    bbox.left + local_left,
                    bbox.top + local_top,
                    bbox.left + local_right,
                    bbox.top + local_bottom,
                )
                tile_pixels = pixels[
                    local_top:local_bottom,
                    local_left:local_right,
                ].copy()
                local_unit_mask = unit_mask[
                    local_top:local_bottom,
                    local_left:local_right,
                ]
                tile_pixels[~local_unit_mask] = 255
                tiles.append(
                    _Tile(
                        unit_id=unit.unit_id,
                        segment_ids=group_ids,
                        source_bbox=source_bbox,
                        pixels=tile_pixels,
                    )
                )

        if tiles:
            payload, placements, packed_pixels, packed_size = _pack_tiles(
                tuple(tiles)
            )
            crop_bbox = Box(0, 0, packed_size[0], packed_size[1])
            occupied_before = sum(
                tile.pixels.shape[0] * tile.pixels.shape[1] for tile in tiles
            )
            compacted.append(
                BlockCompaction(
                    block_id=block.block_id,
                    placements=placements,
                    omitted_empty_units=tuple(dict.fromkeys(omitted)),
                    occupied_pixels_before=occupied_before,
                    packed_canvas_pixels=packed_pixels,
                )
            )
        else:
            payload = _png_bytes(pixels)
        raw = CropInput(f"{block.block_id}-raw", payload)
        outputs.append(
            BlockCropPair(
                block_id=block.block_id,
                bbox=crop_bbox,
                segment_ids=block.segment_ids,
                raw=raw,
                gamma=None,
                isolation_mask_png=None,
                masked_segment_ids=(),
            )
        )
    return tuple(outputs), tuple(compacted)


_TOKEN = re.compile(r"[\w]+", re.UNICODE)
_VOWELS = frozenset(
    "aeiouyAEIOUY"
    "аеёиоуыэюяАЕЁИОУЫЭЮЯ"
    "αεηιουωΑΕΗΙΟΥΩ"
)


def _character_script(character: str) -> str:
    if not character.isalpha():
        return "neutral"
    name = unicodedata.name(character, "")
    for script in (
        "CYRILLIC",
        "LATIN",
        "GREEK",
        "CJK",
        "HIRAGANA",
        "KATAKANA",
    ):
        if script in name:
            return script.lower()
    return "other"


def _allowed_scripts(languages: tuple[str, ...]) -> frozenset[str]:
    scripts = set()
    for language in languages:
        if language in {"rus", "Cyrillic"}:
            scripts.add("cyrillic")
        elif language in {"eng", "Latin"}:
            scripts.add("latin")
        elif language in {"ell", "Greek"}:
            scripts.add("greek")
        elif language in {"chi_sim", "chi_tra", "HanS", "HanT"}:
            scripts.update(("cjk", "hiragana", "katakana"))
        elif language == "equ":
            scripts.update(("latin", "greek"))
    return frozenset(scripts)


def assess_grammar(
    output: OcrEngineOutput,
    languages: tuple[str, ...],
) -> GrammarAssessment:
    text = output.text.strip()
    if not text:
        return GrammarAssessment(0, False, ("empty",))
    confidences = tuple(word.confidence for word in output.words)
    mean_confidence = (
        sum(confidences) / len(confidences) if confidences else 0.0
    )
    minimum_confidence = min(confidences) if confidences else 0.0
    letters = tuple(character for character in text if character.isalpha())
    allowed = _allowed_scripts(languages)
    matching_letters = sum(
        _character_script(character) in allowed for character in letters
    )
    script_fit = matching_letters / len(letters) if letters else 1.0
    controls = sum(
        unicodedata.category(character).startswith("C")
        and not character.isspace()
        for character in text
    )
    visible = tuple(character for character in text if not character.isspace())
    symbols = sum(
        not character.isalnum()
        and character not in ".,:;!?%+-=*/()[]{}<>_|#@"
        for character in visible
    )
    symbol_ratio = symbols / len(visible) if visible else 1.0
    tokens = tuple(_TOKEN.findall(text))
    lexical = tuple(
        token for token in tokens if any(char.isalpha() for char in token)
    )
    implausible = 0
    for token in lexical:
        letters_in_token = tuple(char for char in token if char.isalpha())
        if (
            len(letters_in_token) >= 4
            and not token.isupper()
            and not any(character in _VOWELS for character in letters_in_token)
        ):
            implausible += 1
    lexical_fit = (
        1.0 - implausible / len(lexical)
        if lexical
        else (1.0 if any(character.isdigit() for character in text) else 0.0)
    )
    semantic_shape = bool(letters) or len(tokens) > 1 or any(
        character in "%=+-*/" for character in text
    )
    cyrillic_letters = sum(
        1 for character in text if "\u0400" <= character <= "\u04ff"
    )
    latin_letters = sum(
        1 for character in text if "a" <= character.lower() <= "z"
    )
    allowed_latin_tokens = {"CI", "PR", "README", "SCA", "SBOM"}
    suspicious_latin_tokens = {
        token
        for token in re.findall(r"[A-Za-z]+", text)
        if token.upper() not in allowed_latin_tokens
        and (
            len(token) <= 2
            or (not token.islower() and not token.isupper())
        )
    }
    suspicious_script_mix = (
        cyrillic_letters > latin_letters * 2
        and bool(suspicious_latin_tokens)
    )
    singletons = sum(len(token) == 1 and token.isalpha() for token in lexical)
    singleton_ratio = singletons / len(lexical) if lexical else 0.0
    score = (
        mean_confidence * 0.48
        + script_fit * 0.28
        + lexical_fit * 0.18
        + (1.0 - min(1.0, symbol_ratio * 3.0)) * 0.06
    )
    score -= min(0.35, singleton_ratio * 0.25)
    score -= min(0.50, controls * 0.20)
    percent = max(0, min(99, round(score * 100)))
    exact = bool(
        output.words
        and mean_confidence >= 0.92
        and minimum_confidence >= 0.75
        and script_fit == 1.0
        and lexical_fit == 1.0
        and singleton_ratio <= 0.20
        and symbol_ratio <= 0.05
        and controls == 0
        and semantic_shape
        and not suspicious_script_mix
    )
    reasons = []
    if mean_confidence < 0.92:
        reasons.append("confidence")
    if script_fit < 1.0:
        reasons.append("script")
    if lexical_fit < 1.0:
        reasons.append("t9-shape")
    if singleton_ratio > 0.20:
        reasons.append("singletons")
    if symbol_ratio > 0.05 or controls:
        reasons.append("garbage-symbols")
    if not semantic_shape:
        reasons.append("context-shape")
    if suspicious_script_mix:
        reasons.append("mixed-script-substitution")
    if exact:
        percent = 100
        reasons.append("exact")
    return GrammarAssessment(percent, exact, tuple(reasons))


@dataclass(frozen=True)
class _Candidate:
    profile: LanguageProfile
    transform: OcrTransform
    output: OcrEngineOutput
    assessment: GrammarAssessment
    input_sha256: str
    elapsed_seconds: float


@dataclass(frozen=True)
class _IsolatedRecognition:
    candidate: _Candidate | None
    attempts: tuple[LanguageAttempt, ...]
    observations: tuple[tuple[str, int], ...]
    elapsed_seconds: float


class AdaptivePersistentOcrSession:
    """Sequential language scheduler with bounded membership recursion."""

    def __init__(
        self,
        lanes: tuple[OcrLane, ...],
        *,
        state: LanguageSplayState | None = None,
        log_path: Path | None = None,
    ) -> None:
        if len(lanes) != 1:
            raise ValueError("adaptive OCR requires exactly one base lane")
        self._profiles = self._profiles_from_lane(lanes[0])
        self._max_workers = lanes[0].max_workers
        self.state = state or LanguageSplayState(self._profiles)
        self.state.ensure_profiles(self._profiles)
        self.log_path = log_path
        self._workers: dict[str, object] = {}
        self._worker_errors: dict[str, Exception] = {}
        self._enhancer = GammaDarkCropEnhancer()
        self._closed = False

    @staticmethod
    def _profiles_from_lane(lane: OcrLane) -> tuple[LanguageProfile, ...]:
        prototype = lane.worker_factory()
        try:
            config = getattr(prototype, "config", None)
            if config is None:
                config = getattr(prototype, "_config", None)
            if config is None or not hasattr(config, "languages"):
                raise TypeError(
                    "adaptive OCR worker must expose a dataclass language config"
                )
            adapter_type = type(prototype)
            hinted = tuple(getattr(config, "languages"))
        finally:
            close = getattr(prototype, "close", None)
            if callable(close):
                close()

        ordered_languages = [
            ("rus", "eng"),
            ("rus",),
            ("eng",),
            ("chi_sim",),
            ("ell",),
            ("equ",),
        ]
        for language in hinted:
            candidate = tuple(
                item for item in str(language).split("+") if item
            )
            if candidate and candidate not in ordered_languages:
                ordered_languages.append(candidate)

        profiles = []
        for languages in ordered_languages:
            profile_id = "-".join(languages)
            profile_config = replace(config, languages=languages)

            def factory(
                profile_config: object = profile_config,
                adapter_type: type = adapter_type,
            ) -> object:
                return adapter_type(config=profile_config)

            profiles.append(
                LanguageProfile(
                    profile_id=profile_id,
                    languages=languages,
                    resource=lane.resource,
                    worker_factory=factory,
                )
            )
        return tuple(profiles)

    def __enter__(self) -> AdaptivePersistentOcrSession:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _worker(self, profile: LanguageProfile) -> object:
        if profile.profile_id in self._worker_errors:
            raise self._worker_errors[profile.profile_id]
        worker = self._workers.get(profile.profile_id)
        if worker is not None:
            return worker
        try:
            worker = profile.worker_factory()
        except Exception as exc:
            self._worker_errors[profile.profile_id] = exc
            raise
        if not callable(getattr(worker, "recognize", None)):
            error = TypeError("language worker has no recognize()")
            self._worker_errors[profile.profile_id] = error
            raise error
        self._workers[profile.profile_id] = worker
        return worker

    @staticmethod
    def _mean_confidence(output: OcrEngineOutput) -> float:
        return (
            sum(word.confidence for word in output.words) / len(output.words)
            if output.words
            else 0.0
        )

    def _attempt(
        self,
        *,
        block_id: str,
        unit_id: str,
        profile: LanguageProfile,
        transform: OcrTransform,
        raw_png: bytes,
    ) -> _Candidate | None:
        started = time.perf_counter()
        try:
            if transform is OcrTransform.GAMMA:
                enhanced = self._enhancer.enhance_many(
                    (CropInput(f"{block_id}-gamma-attempt", raw_png),)
                )[0]
                payload = enhanced.png_bytes
            else:
                payload = raw_png
            output = self._worker(profile).recognize(payload)
            if not isinstance(output, OcrEngineOutput):
                raise TypeError("language worker returned a non-OCR output")
            assessment = assess_grammar(output, profile.languages)
            elapsed = time.perf_counter() - started
            self.state.record(
                block_id=block_id,
                unit_id=unit_id,
                profile=profile,
                transform=transform,
                status="complete",
                assessment=assessment,
                mean_confidence=self._mean_confidence(output),
                text=output.text,
                error="",
                elapsed_seconds=elapsed,
            )
            return _Candidate(
                profile=profile,
                transform=transform,
                output=output,
                assessment=assessment,
                input_sha256=hashlib.sha256(payload).hexdigest(),
                elapsed_seconds=elapsed,
            )
        except Exception as exc:
            elapsed = time.perf_counter() - started
            assessment = GrammarAssessment(0, False, ("ocr-error",))
            self.state.record(
                block_id=block_id,
                unit_id=unit_id,
                profile=profile,
                transform=transform,
                status="failed",
                assessment=assessment,
                mean_confidence=0.0,
                text="",
                error=f"{type(exc).__name__}: {exc}",
                elapsed_seconds=elapsed,
            )
            return None

    def _recognize_image(
        self,
        *,
        block_id: str,
        unit_id: str,
        raw_png: bytes,
        use_locked_profile: bool = True,
    ) -> _Candidate | None:
        profiles_by_id = {
            profile.profile_id: profile for profile in self._profiles
        }
        best: _Candidate | None = None
        locked_profile_id = (
            self.state.locked_profile_id if use_locked_profile else None
        )
        profile_ids = (
            (locked_profile_id,)
            if locked_profile_id in profiles_by_id
            else self.state.ordered_profile_ids()
        )
        for profile_id in profile_ids:
            profile = profiles_by_id.get(profile_id)
            if profile is None:
                continue
            profile_best: _Candidate | None = None
            for transform in (OcrTransform.RAW, OcrTransform.GAMMA):
                candidate = self._attempt(
                    block_id=block_id,
                    unit_id=unit_id,
                    profile=profile,
                    transform=transform,
                    raw_png=raw_png,
                )
                if candidate is not None and (
                    profile_best is None
                    or (
                        candidate.assessment.percent,
                        self._mean_confidence(candidate.output),
                        len(candidate.output.text),
                    )
                    > (
                        profile_best.assessment.percent,
                        self._mean_confidence(profile_best.output),
                        len(profile_best.output.text),
                    )
                ):
                    profile_best = candidate
                if candidate is not None and candidate.assessment.exact:
                    break
            self.state.observe(
                profile.profile_id,
                profile_best.assessment.percent if profile_best is not None else 0,
            )
            if profile_best is not None and (
                best is None
                or (
                    profile_best.assessment.percent,
                    self._mean_confidence(profile_best.output),
                    len(profile_best.output.text),
                )
                > (
                    best.assessment.percent,
                    self._mean_confidence(best.output),
                    len(best.output.text),
                )
            ):
                best = profile_best
            if profile_best is not None and (
                profile_best.assessment.exact
            ):
                break
        return best

    def _recognize_isolated(
        self,
        *,
        block_id: str,
        unit_id: str,
        raw_png: bytes,
        nodes: tuple[SplayLanguageNode, ...],
        locked_profile_id: str | None,
    ) -> _IsolatedRecognition:
        started = time.perf_counter()
        child = object.__new__(AdaptivePersistentOcrSession)
        child._profiles = self._profiles
        child._max_workers = 1
        child.state = LanguageSplayState(self._profiles)
        child.state.nodes = [replace(node) for node in nodes]
        child.state.locked_profile_id = locked_profile_id
        child.log_path = None
        child._workers = {}
        child._worker_errors = {}
        child._enhancer = GammaDarkCropEnhancer()
        child._closed = False
        baseline_attempts = {
            node.profile_id: node.attempts for node in nodes
        }
        try:
            candidate = child._recognize_image(
                block_id=block_id,
                unit_id=unit_id,
                raw_png=raw_png,
            )
            attempted_profiles = tuple(
                dict.fromkeys(
                    attempt.profile_id for attempt in child.state.attempts
                )
            )
            node_by_id = {
                node.profile_id: node for node in child.state.nodes
            }
            observations = tuple(
                (
                    profile_id,
                    node_by_id[profile_id].last_grammar_percent,
                )
                for profile_id in attempted_profiles
                if node_by_id[profile_id].attempts
                > baseline_attempts.get(profile_id, 0)
            )
            return _IsolatedRecognition(
                candidate=candidate,
                attempts=tuple(child.state.attempts),
                observations=observations,
                elapsed_seconds=time.perf_counter() - started,
            )
        finally:
            child.close()

    def _recognize_blocks(
        self,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
    ) -> tuple[tuple[_Candidate | None, float], ...]:
        if not plan.blocks:
            return ()
        first_started = time.perf_counter()
        first = self._recognize_image(
            block_id=plan.blocks[0].block_id,
            unit_id="full-block",
            raw_png=crops[0].raw.png_bytes,
        )
        if first is not None and first.assessment.exact:
            self.state.locked_profile_id = first.profile.profile_id
        results: list[tuple[_Candidate | None, float]] = [
            (first, time.perf_counter() - first_started)
        ]
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_workers
        ) as executor:
            for start in range(1, len(plan.blocks), self._max_workers):
                stop = min(len(plan.blocks), start + self._max_workers)
                nodes = tuple(replace(node) for node in self.state.nodes)
                futures = tuple(
                    executor.submit(
                        self._recognize_isolated,
                        block_id=plan.blocks[index].block_id,
                        unit_id="full-block",
                        raw_png=crops[index].raw.png_bytes,
                        nodes=nodes,
                        locked_profile_id=self.state.locked_profile_id,
                    )
                    for index in range(start, stop)
                )
                isolated = tuple(future.result() for future in futures)
                for item in isolated:
                    self.state.merge_attempts(item.attempts)
                    for profile_id, grammar_percent in item.observations:
                        self.state.observe(profile_id, grammar_percent)
                    results.append(
                        (item.candidate, item.elapsed_seconds)
                    )
        return tuple(results)

    @staticmethod
    def _intersection_area(left: Box, right: Box) -> int:
        width = max(
            0, min(left.right, right.right) - max(left.left, right.left)
        )
        height = max(
            0, min(left.bottom, right.bottom) - max(left.top, right.top)
        )
        return width * height

    @staticmethod
    def _map_word(
        word: OcrWord,
        placement: CompactionPlacement,
        block_bbox: Box,
    ) -> OcrWord:
        crop = placement.crop_bbox
        source = placement.source_bbox
        scale_x = source.width / max(1, crop.width)
        scale_y = source.height / max(1, crop.height)
        left = source.left - block_bbox.left + round(
            (word.bbox.left - crop.left) * scale_x
        )
        top = source.top - block_bbox.top + round(
            (word.bbox.top - crop.top) * scale_y
        )
        right = source.left - block_bbox.left + round(
            (word.bbox.right - crop.left) * scale_x
        )
        bottom = source.top - block_bbox.top + round(
            (word.bbox.bottom - crop.top) * scale_y
        )
        left = max(0, min(block_bbox.width - 1, left))
        top = max(0, min(block_bbox.height - 1, top))
        right = max(left + 1, min(block_bbox.width, right))
        bottom = max(top + 1, min(block_bbox.height, bottom))
        return OcrWord(
            text=word.text,
            bbox=Box(left, top, right, bottom),
            confidence=word.confidence,
        )

    def _map_full_output(
        self,
        output: OcrEngineOutput,
        compaction: BlockCompaction | None,
        block_bbox: Box,
    ) -> OcrEngineOutput:
        if compaction is None or not compaction.placements or not output.words:
            return output
        mapped = []
        for word in output.words:
            placement = max(
                compaction.placements,
                key=lambda item: (
                    self._intersection_area(word.bbox, item.crop_bbox),
                    -abs(
                        (word.bbox.left + word.bbox.right)
                        - (item.crop_bbox.left + item.crop_bbox.right)
                    ),
                ),
            )
            mapped.append(self._map_word(word, placement, block_bbox))
        return OcrEngineOutput(
            text=" ".join(word.text for word in mapped),
            words=tuple(mapped),
            geometry=OcrOutputGeometry.WORD_BOXES,
        )

    def _recursive_candidate(
        self,
        *,
        block_id: str,
        crop: BlockCropPair,
        compaction: BlockCompaction,
    ) -> _Candidate | None:
        with Image.open(io.BytesIO(crop.raw.png_bytes)) as opened:
            opened.load()
            image = opened.convert("RGB")
        candidates = []
        try:
            for placement in compaction.placements:
                tile = image.crop(placement.crop_bbox.as_tuple())
                try:
                    payload = io.BytesIO()
                    tile.save(payload, format="PNG", compress_level=1)
                    candidate = self._recognize_image(
                        block_id=block_id,
                        unit_id=placement.unit_id,
                        raw_png=payload.getvalue(),
                        use_locked_profile=False,
                    )
                finally:
                    tile.close()
                if candidate is None:
                    continue
                local_placement = replace(
                    placement,
                    crop_bbox=Box(
                        0,
                        0,
                        placement.crop_bbox.width,
                        placement.crop_bbox.height,
                    ),
                )
                mapped = tuple(
                    self._map_word(word, local_placement, crop.bbox)
                    for word in candidate.output.words
                )
                candidates.append((placement, candidate, mapped))
        finally:
            image.close()
        if not candidates:
            return None
        ordered_candidates = sorted(
            candidates,
            key=lambda item: (
                item[0].source_bbox.top,
                item[0].source_bbox.left,
            ),
        )
        words = tuple(
            word
            for _, _, mapped in ordered_candidates
            for word in mapped
        )
        if not words:
            return None
        total_characters = sum(
            max(1, len(candidate.output.text))
            for _, candidate, _ in candidates
        )
        grammar_percent = round(
            sum(
                candidate.assessment.percent
                * max(1, len(candidate.output.text))
                for _, candidate, _ in candidates
            )
            / total_characters
        )
        representative = max(
            (candidate for _, candidate, _ in candidates),
            key=lambda candidate: (
                candidate.assessment.percent,
                len(candidate.output.text),
            ),
        )
        output = OcrEngineOutput(
            text=" ".join(word.text for word in words),
            words=words,
            geometry=OcrOutputGeometry.WORD_BOXES,
        )
        return _Candidate(
            profile=representative.profile,
            transform=representative.transform,
            output=output,
            assessment=GrammarAssessment(
                grammar_percent,
                grammar_percent == 100,
                ("recursive-membership-units",),
            ),
            input_sha256=hashlib.sha256(crop.raw.png_bytes).hexdigest(),
            elapsed_seconds=sum(
                candidate.elapsed_seconds
                for _, candidate, _ in candidates
            ),
        )

    @staticmethod
    def _observed_scripts(text: str) -> frozenset[str]:
        return frozenset(
            script
            for character in text
            if (script := _character_script(character)) != "neutral"
        )

    def run_with_compaction(
        self,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        compactions: tuple[BlockCompaction, ...],
    ) -> OcrQueueResult:
        if self._closed:
            raise RuntimeError("adaptive OCR session is closed")
        compaction_by_id = {item.block_id: item for item in compactions}
        jobs = []
        diagnostics = []
        recognized = self._recognize_blocks(plan=plan, crops=crops)
        for index, (block, crop) in enumerate(zip(plan.blocks, crops)):
            candidate, recognition_seconds = recognized[index]
            recursive_started = time.perf_counter()
            compaction = compaction_by_id.get(block.block_id)
            if candidate is not None:
                scripts = self._observed_scripts(candidate.output.text)
                if (
                    compaction is not None
                    and 1 < len(compaction.placements) <= 16
                    and not candidate.assessment.exact
                    and len(scripts) > 1
                ):
                    recursive = self._recursive_candidate(
                        block_id=block.block_id,
                        crop=crop,
                        compaction=compaction,
                    )
                    if recursive is not None:
                        candidate = recursive
                        diagnostics.append(
                            f"block={block.block_id};recursive=membership-units"
                        )
            if candidate is None:
                jobs.append(
                    OcrJobResult(
                        job_id=f"ocr-job-{index:08d}",
                        block_id=block.block_id,
                        transform=OcrTransform.RAW,
                        lane_id="adaptive-language",
                        resource=OcrResource.CPU,
                        status=OcrJobStatus.FAILED,
                        output=None,
                        error_type="OcrRecognitionMissError",
                        error_message="all sequential language profiles failed",
                        elapsed_seconds=(
                            recognition_seconds
                            + time.perf_counter()
                            - recursive_started
                        ),
                        input_sha256=hashlib.sha256(
                            crop.raw.png_bytes
                        ).hexdigest(),
                        context_sha256=hashlib.sha256(
                            crop.raw.png_bytes
                        ).hexdigest(),
                        failure_code=OcrFailureCode.RECOGNITION_MISS,
                        capability_id="adaptive-language",
                    )
                )
                continue
            mapped_output = (
                candidate.output
                if "recursive-membership-units" in candidate.assessment.reasons
                else self._map_full_output(
                    candidate.output,
                    compaction,
                    block.bbox,
                )
            )
            jobs.append(
                OcrJobResult(
                    job_id=f"ocr-job-{index:08d}",
                    block_id=block.block_id,
                    transform=candidate.transform,
                    lane_id=candidate.profile.profile_id,
                    resource=candidate.profile.resource,
                    status=OcrJobStatus.COMPLETE,
                    output=mapped_output,
                    error_type=None,
                    error_message=None,
                    elapsed_seconds=(
                        recognition_seconds
                        + time.perf_counter()
                        - recursive_started
                    ),
                    input_sha256=candidate.input_sha256,
                    context_sha256=hashlib.sha256(
                        crop.raw.png_bytes
                    ).hexdigest(),
                    failure_code=None,
                    capability_id=candidate.profile.profile_id,
                )
            )
            diagnostics.append(
                f"block={block.block_id};profile={candidate.profile.profile_id};"
                f"grammar={candidate.assessment.percent};"
                f"transform={candidate.transform.value}"
            )
        if self.log_path is not None:
            self.state.write_csv(self.log_path)
        complete = sum(job.status is OcrJobStatus.COMPLETE for job in jobs)
        failed = len(jobs) - complete
        return OcrQueueResult(
            jobs=tuple(jobs),
            status=(
                OcrQueueStatus.COMPLETE
                if failed == 0
                else OcrQueueStatus.PARTIAL
            ),
            complete=complete,
            failed=failed,
            diagnostics=tuple(diagnostics),
        )

    def run(
        self,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
    ) -> OcrQueueResult:
        return self.run_with_compaction(
            plan=plan,
            crops=crops,
            compactions=(),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for worker in self._workers.values():
            close = getattr(worker, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        if self.log_path is not None:
            self.state.write_csv(self.log_path)


__all__ = [
    "AdaptivePersistentOcrSession",
    "BlockCompaction",
    "CompactionPlacement",
    "GrammarAssessment",
    "LanguageProfile",
    "LanguageSplayState",
    "assess_grammar",
    "build_deferred_compact_crops",
]
