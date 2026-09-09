from __future__ import annotations

import difflib
import concurrent.futures
import csv
import dataclasses
import enum
import hashlib
import io
import json
import math
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping

import numpy as np
from PIL import Image

from app.sparse_pipeline.block_crops import BlockCropPair
from app.sparse_pipeline.block_planning import (
    BlockPlan,
    BlockPlanningLimitError,
    RecognitionBlock,
)
from app.sparse_pipeline.contracts import Box, SegmentSpan
from app.sparse_pipeline.crop_enhancement import (
    CropInput,
    GammaDarkCropEnhancer,
)
from app.sparse_pipeline.ocr_adapter_contracts import (
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


def _cache_json_value(value: object) -> object:
    if dataclasses.is_dataclass(value):
        return {field.name: _cache_json_value(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Path):
        return str(value.resolve())
    if isinstance(value, Mapping):
        return {str(key): _cache_json_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [_cache_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_cache_json_value(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _cache_fingerprint(value: object) -> str:
    payload = json.dumps(
        _cache_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class OcrContentCacheKey:
    """Identity of one actual adapter call, independent of debug block IDs."""

    actual_png_sha256: str
    adapter_id: str
    config_sha256: str
    profile_id: str
    transform: str
    psm: str
    enhancer_id: str


@dataclass(frozen=True)
class OcrContentCacheStats:
    requests: int = 0
    hits: int = 0
    misses: int = 0
    waits: int = 0
    entries: int = 0
    ocr_work_seconds: float = 0.0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "requests": self.requests,
            "hits": self.hits,
            "misses": self.misses,
            "waits": self.waits,
            "entries": self.entries,
            "exact_duplicate_calls_avoided": self.hits,
            "ocr_work_seconds": self.ocr_work_seconds,
        }


class AdaptiveOcrContentCache:
    """Thread-safe, session-scoped cache of successful OCR adapter calls."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._entries: dict[OcrContentCacheKey, OcrEngineOutput] = {}
        self._inflight: set[OcrContentCacheKey] = set()
        self._requests = 0
        self._hits = 0
        self._misses = 0
        self._waits = 0
        self._ocr_work_seconds = 0.0

    def recognize(
        self,
        key: OcrContentCacheKey,
        operation: Callable[[], OcrEngineOutput],
    ) -> tuple[OcrEngineOutput, bool]:
        with self._condition:
            self._requests += 1
            while True:
                cached = self._entries.get(key)
                if cached is not None:
                    self._hits += 1
                    return cached, True
                if key not in self._inflight:
                    self._inflight.add(key)
                    self._misses += 1
                    break
                self._waits += 1
                self._condition.wait()
        started = time.perf_counter()
        try:
            output = operation()
        except Exception:
            with self._condition:
                self._inflight.remove(key)
                self._condition.notify_all()
            raise
        work_seconds = time.perf_counter() - started
        with self._condition:
            self._entries[key] = output
            self._ocr_work_seconds += work_seconds
            self._inflight.remove(key)
            self._condition.notify_all()
        return output, False

    def snapshot(self) -> OcrContentCacheStats:
        with self._condition:
            return OcrContentCacheStats(
                requests=self._requests,
                hits=self._hits,
                misses=self._misses,
                waits=self._waits,
                entries=len(self._entries),
                ocr_work_seconds=self._ocr_work_seconds,
            )


@dataclass(frozen=True)
class CompactionPlacement:
    unit_id: str
    segment_ids: tuple[str, ...]
    source_bbox: Box
    crop_bbox: Box


@dataclass(frozen=True)
class SourcePlacementArtifact:
    unit_id: str
    segment_ids: tuple[str, ...]
    source_bbox: Box
    png_bytes: bytes
    sha256: str
    island_id: str
    matrix_row: int
    matrix_column: int
    polar_order: int

    def __post_init__(self) -> None:
        if not self.unit_id or not self.segment_ids:
            raise ValueError("source placement identifiers must not be empty")
        if not self.png_bytes:
            raise ValueError("source placement artifact must not be empty")
        if hashlib.sha256(self.png_bytes).hexdigest() != self.sha256:
            raise ValueError("source placement artifact digest mismatch")
        with Image.open(io.BytesIO(self.png_bytes)) as opened:
            opened.load()
            if opened.format != "PNG" or opened.size != (
                self.source_bbox.width,
                self.source_bbox.height,
            ):
                raise ValueError("source placement artifact disagrees with source bbox")


@dataclass(frozen=True)
class TopologyScriptEvidence:
    script_kind: str
    matrix_sha256: str
    island_id: str
    matrix_columns: tuple[int, ...]
    source_left_ppm: int
    source_right_ppm: int
    confidence: float


@dataclass(frozen=True)
class TopologyFusionSpanProvenance:
    block_id: str
    unit_id: str
    script_kind: str
    text: str
    source_bbox: Box
    output_bbox: Box
    profile_id: str
    transform: OcrTransform
    input_sha256: str


@dataclass(frozen=True)
class TopologySourceFusionResult:
    queue: OcrQueueResult
    changed_jobs: tuple[str, ...]
    provenance: tuple[TopologyFusionSpanProvenance, ...]
    elapsed_seconds: float
    cache_metrics: Mapping[str, int | float]


class CompactionRasterKind(enum.Enum):
    SPATIAL_PACKED = "spatial-packed-v1"
    CANONICAL_LOCALITY = "canonical-locality-v1"


@dataclass(frozen=True)
class BlockCompaction:
    block_id: str
    placements: tuple[CompactionPlacement, ...]
    omitted_empty_units: tuple[str, ...]
    occupied_pixels_before: int
    occupied_pixels_after: int
    packed_canvas_pixels: int
    raster_kind: CompactionRasterKind = CompactionRasterKind.SPATIAL_PACKED
    raw_sha256: str = ""
    source_placements: tuple[SourcePlacementArtifact, ...] = ()


@dataclass(frozen=True)
class CanonicalLocalityRaster:
    png_bytes: bytes
    placements: tuple[CompactionPlacement, ...]
    sha256: str
    packed_canvas_pixels: int
    size: tuple[int, int]


def _membership_component_by_block_id(plan: BlockPlan) -> dict[str, str]:
    """Return stable overlap components without making scope IDs global."""

    block_ids = tuple(block.block_id for block in plan.blocks)
    parent = {block_id: block_id for block_id in block_ids}
    order = {block_id: index for index, block_id in enumerate(block_ids)}

    def find(block_id: str) -> str:
        while parent[block_id] != block_id:
            parent[block_id] = parent[parent[block_id]]
            block_id = parent[block_id]
        return block_id

    def union(first: str, second: str) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root == second_root:
            return
        if order[first_root] <= order[second_root]:
            parent[second_root] = first_root
        else:
            parent[first_root] = second_root

    for unit in plan.membership_units:
        members = tuple(dict.fromkeys(block_id for block_id in unit.block_ids if block_id in parent))
        for block_id in members[1:]:
            union(members[0], block_id)
    return {block_id: find(block_id) for block_id in block_ids}


def _component_membership_slots(
    plan: BlockPlan,
) -> dict[str, tuple[dict[str, int], tuple[int, int]]]:
    component_by_block_id = _membership_component_by_block_id(plan)
    unit_ids_by_component: dict[str, list[str]] = {}
    for unit in plan.membership_units:
        component_ids = {
            component_by_block_id[block_id] for block_id in unit.block_ids if block_id in component_by_block_id
        }
        if len(component_ids) > 1:
            raise ValueError("membership unit spans disconnected block components")
        if not component_ids:
            continue
        component_id = next(iter(component_ids))
        unit_ids_by_component.setdefault(component_id, []).append(unit.unit_id)
    contracts: dict[str, tuple[dict[str, int], tuple[int, int]]] = {}
    for component_id, unit_ids in unit_ids_by_component.items():
        ordered = tuple(dict.fromkeys(unit_ids))
        columns = max(1, min(16, math.ceil(len(ordered) / 16)))
        rows = math.ceil(len(ordered) / columns)
        contracts[component_id] = (
            {unit_id: slot for slot, unit_id in enumerate(ordered)},
            (columns, rows),
        )
    return contracts


@dataclass(frozen=True)
class LanguageProfile:
    profile_id: str
    languages: tuple[str, ...]
    resource: OcrResource
    worker_factory: Callable[[], object]
    adapter_id: str
    config_sha256: str
    psm: str


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


@dataclass
class EngineOrderSplayNode:
    languages: tuple[str, ...]
    initial_rank: int
    attempts: int = 0
    wins: int = 0
    loss_streak: int = 0
    quality_sum: float = 0.0


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
    input_sha256: str
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
        self.observations: list[tuple[str, int]] = []
        self.locked_profile_id: str | None = None
        self.lock_is_provisional = False
        self.engine_order_nodes: dict[str, list[EngineOrderSplayNode]] = {}
        self.fallback_loss_streaks: dict[str, int] = {}
        self._sequence = 0
        self._capture_attempt_inputs = False
        self._attempt_input_payloads: dict[str, bytes] = {}
        self._written_attempt_inputs: set[str] = set()
        self._attempt_input_lock = threading.Lock()

    def enable_attempt_input_artifacts(self) -> None:
        self._capture_attempt_inputs = True

    def share_attempt_input_artifacts(
        self,
        source: LanguageSplayState,
    ) -> None:
        self._capture_attempt_inputs = source._capture_attempt_inputs
        self._attempt_input_payloads = source._attempt_input_payloads
        self._written_attempt_inputs = source._written_attempt_inputs
        self._attempt_input_lock = source._attempt_input_lock

    def remember_attempt_input(self, digest: str, payload: bytes) -> None:
        if not self._capture_attempt_inputs:
            return
        actual_digest = hashlib.sha256(payload).hexdigest()
        if actual_digest != digest:
            raise ValueError("OCR attempt input digest disagrees with payload")
        with self._attempt_input_lock:
            previous = self._attempt_input_payloads.setdefault(digest, payload)
            if previous != payload:
                raise ValueError("OCR attempt digest identifies different payloads")

    def attempt_input_payload(self, digest: str) -> bytes | None:
        with self._attempt_input_lock:
            return self._attempt_input_payloads.get(digest)

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

    def ensure_engine_orders(
        self,
        profile_id: str,
        orders: tuple[tuple[str, ...], ...],
    ) -> None:
        nodes = self.engine_order_nodes.setdefault(profile_id, [])
        existing = {node.languages for node in nodes}
        for languages in orders:
            if languages not in existing:
                nodes.append(EngineOrderSplayNode(languages, len(nodes)))
                existing.add(languages)

    def copy_language_strategy_from(
        self,
        source: LanguageSplayState,
    ) -> None:
        self.engine_order_nodes = {
            profile_id: [replace(node) for node in nodes] for profile_id, nodes in source.engine_order_nodes.items()
        }
        self.fallback_loss_streaks = dict(source.fallback_loss_streaks)

    def ordered_engine_orders(
        self,
        profile_id: str,
        orders: tuple[tuple[str, ...], ...],
        *,
        maximum_losses: int,
    ) -> tuple[tuple[str, ...], ...]:
        self.ensure_engine_orders(profile_id, orders)
        nodes = self.engine_order_nodes[profile_id]
        active = tuple(node.languages for node in nodes if node.loss_streak < maximum_losses)
        return active or (nodes[0].languages,)

    def observe_engine_order(
        self,
        profile_id: str,
        attempted_orders: tuple[tuple[str, ...], ...],
        winner: tuple[str, ...],
        quality_by_order: dict[tuple[str, ...], float],
    ) -> None:
        self.ensure_engine_orders(profile_id, attempted_orders)
        nodes = self.engine_order_nodes[profile_id]
        attempted = frozenset(attempted_orders)
        for node in nodes:
            if node.languages not in attempted:
                continue
            node.attempts += 1
            node.quality_sum += quality_by_order.get(node.languages, 0.0)
            if node.languages == winner:
                node.wins += 1
                node.loss_streak = 0
            else:
                node.loss_streak += 1
        nodes.sort(
            key=lambda node: (
                -node.wins,
                -(node.quality_sum / max(1, node.attempts)),
                node.loss_streak,
                node.initial_rank,
            )
        )

    def finalize_engine_order_calibration(
        self,
        profile_id: str,
        *,
        minimum_samples: int,
        maximum_losses: int,
    ) -> None:
        nodes = self.engine_order_nodes.get(profile_id, [])
        eligible = tuple(node for node in nodes if node.attempts >= minimum_samples)
        if len(eligible) < 2:
            return
        winner = max(
            eligible,
            key=lambda node: (
                node.wins,
                node.quality_sum / max(1, node.attempts),
                -node.initial_rank,
            ),
        )
        for node in eligible:
            if node is not winner:
                node.loss_streak = maximum_losses
        nodes.sort(
            key=lambda node: (
                node is not winner,
                node.loss_streak,
                node.initial_rank,
            )
        )

    def active_fallback_profile_ids(
        self,
        profile_ids: tuple[str, ...],
        *,
        maximum_losses: int,
    ) -> tuple[str, ...]:
        return tuple(
            profile_id for profile_id in profile_ids if self.fallback_loss_streaks.get(profile_id, 0) < maximum_losses
        )

    def observe_fallback_profiles(
        self,
        attempted_profile_ids: tuple[str, ...],
        winner_profile_id: str,
        primary_profile_id: str,
    ) -> None:
        for profile_id in attempted_profile_ids:
            if profile_id == primary_profile_id:
                continue
            if winner_profile_id == profile_id:
                self.fallback_loss_streaks[profile_id] = 0
            elif winner_profile_id == primary_profile_id:
                self.fallback_loss_streaks[profile_id] = self.fallback_loss_streaks.get(profile_id, 0) + 1

    def reset_language_guards(self) -> None:
        for nodes in self.engine_order_nodes.values():
            for node in nodes:
                node.loss_streak = 0
        self.fallback_loss_streaks.clear()

    def ordered_profile_ids(self) -> tuple[str, ...]:
        return tuple(node.profile_id for node in self.nodes)

    def observe(self, profile_id: str, grammar_percent: int) -> None:
        self.observations.append((profile_id, grammar_percent))
        node = next(node for node in self.nodes if node.profile_id == profile_id)
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
        input_sha256: str,
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
                input_sha256=input_sha256,
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
            self.attempts.append(replace(attempt, sequence=self._sequence))

    def write_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        artifact_directory = path.with_name(f"{path.stem}-attempt-inputs")
        with self._attempt_input_lock:
            pending_artifacts = tuple(
                (digest, payload)
                for digest, payload in self._attempt_input_payloads.items()
                if digest not in self._written_attempt_inputs
            )
        if pending_artifacts:
            artifact_directory.mkdir(parents=True, exist_ok=True)
            written = []
            for digest, payload in sorted(pending_artifacts):
                artifact_path = artifact_directory / f"{digest}.png"
                if artifact_path.exists():
                    existing = artifact_path.read_bytes()
                    if hashlib.sha256(existing).hexdigest() != digest:
                        raise ValueError("stored OCR attempt artifact digest is invalid")
                else:
                    artifact_path.write_bytes(payload)
                written.append(digest)
            with self._attempt_input_lock:
                self._written_attempt_inputs.update(written)
        total_weight = sum(node.weight for node in self.nodes) or 1.0
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                (
                    "rank",
                    "profile",
                    "languages",
                    "locked",
                    "lock_kind",
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
                        "yes" if node.profile_id == self.locked_profile_id else "no",
                        (
                            ("provisional" if self.lock_is_provisional else "confirmed")
                            if node.profile_id == self.locked_profile_id
                            else ""
                        ),
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
                    "input_sha256",
                    "input_artifact",
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
                        attempt.input_sha256,
                        (
                            f"{artifact_directory.name}/" f"{attempt.input_sha256}.png"
                            if attempt.input_sha256 in self._attempt_input_payloads
                            else ""
                        ),
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
    canonical_bbox: Box | None = None
    canonical_png_bytes: bytes | None = None


def _png_bytes(pixels: np.ndarray) -> bytes:
    output = io.BytesIO()
    image = Image.fromarray(pixels.astype(np.uint8, copy=False), mode="RGB")
    try:
        image.save(output, format="PNG", compress_level=1, dpi=(300, 300))
    finally:
        image.close()
    return output.getvalue()


def _content_ink_mask(
    pixels: np.ndarray,
    unit_mask: np.ndarray,
) -> tuple[np.ndarray, bool]:
    """Return text-like pixels without flat cell fills or long table rules."""

    luminance = np.rint(
        pixels[:, :, 0].astype(np.float32) * 0.299
        + pixels[:, :, 1].astype(np.float32) * 0.587
        + pixels[:, :, 2].astype(np.float32) * 0.114
    ).astype(np.uint8)
    unit_rows, unit_columns = np.nonzero(unit_mask)
    if not len(unit_rows):
        return np.zeros(unit_mask.shape, dtype=bool), True
    sample_left = max(0, int(unit_columns.min()) - 4)
    sample_top = max(0, int(unit_rows.min()) - 4)
    sample_right = min(unit_mask.shape[1], int(unit_columns.max()) + 5)
    sample_bottom = min(unit_mask.shape[0], int(unit_rows.max()) + 5)
    sample_mask = unit_mask[
        sample_top:sample_bottom,
        sample_left:sample_right,
    ]
    sample_luminance = luminance[
        sample_top:sample_bottom,
        sample_left:sample_right,
    ]
    inside_values = luminance[unit_mask]
    surrounding_values = sample_luminance[~sample_mask]
    sample_density = float(np.count_nonzero(sample_mask)) / max(
        1,
        sample_mask.size,
    )
    values = (
        inside_values
        if sample_density >= 0.45
        else surrounding_values if surrounding_values.size >= 16 else inside_values
    )
    if not values.size:
        return np.zeros(unit_mask.shape, dtype=bool), True

    histogram = np.bincount(values, minlength=256)
    background = int(np.argmax(histogram))
    dark_on_light = background >= 128
    if dark_on_light:
        ink = unit_mask & (luminance <= max(0, background - 12))
    else:
        ink = unit_mask & (luminance >= min(255, background + 12))

    height, width = unit_mask.shape
    minimum_horizontal_rule = max(32, math.ceil(width * 0.65))
    minimum_vertical_rule = max(32, math.ceil(height * 0.65))
    horizontal_rules = np.count_nonzero(ink, axis=1) >= minimum_horizontal_rule
    vertical_rules = np.count_nonzero(ink, axis=0) >= minimum_vertical_rule
    if np.any(horizontal_rules):
        ink[horizontal_rules, :] = False
    if np.any(vertical_rules):
        ink[:, vertical_rules] = False
    return ink, dark_on_light


def _dilate_mask(mask: np.ndarray) -> np.ndarray:
    padded = np.pad(mask, 1, mode="constant", constant_values=False)
    dilated = np.zeros_like(mask)
    for row_offset in range(3):
        for column_offset in range(3):
            dilated |= padded[
                row_offset : row_offset + mask.shape[0],
                column_offset : column_offset + mask.shape[1],
            ]
    return dilated


def _compact_internal_whitespace(
    pixels: np.ndarray,
    ink_mask: np.ndarray,
) -> np.ndarray:
    """Remove long blank bands while retaining readable intra-text spacing."""

    if pixels.shape[:2] != ink_mask.shape:
        raise ValueError("tile pixels and ink mask must have identical geometry")
    if not np.any(ink_mask):
        return pixels

    def retained_indexes(active: np.ndarray, maximum_gap: int) -> np.ndarray:
        occupied = np.flatnonzero(active)
        keep = np.ones(active.shape[0], dtype=bool)
        for first, second in zip(occupied, occupied[1:]):
            blank = int(second - first - 1)
            if blank <= maximum_gap:
                continue
            left_context = maximum_gap // 2
            right_context = maximum_gap - left_context
            keep[int(first) + 1 + left_context : int(second) - right_context] = False
        return np.flatnonzero(keep)

    height, width = ink_mask.shape
    column_gap = max(8, min(32, math.ceil(height * 0.75)))
    row_gap = max(6, min(24, math.ceil(width * 0.08)))
    rows = retained_indexes(np.any(ink_mask, axis=1), row_gap)
    columns = retained_indexes(np.any(ink_mask, axis=0), column_gap)
    return pixels[np.ix_(rows, columns)].copy()


def _pack_tiles(
    tiles: tuple[_Tile, ...],
) -> tuple[
    bytes,
    tuple[CompactionPlacement, ...],
    int,
    tuple[int, int],
]:
    if any(tile.canonical_bbox is not None or tile.canonical_png_bytes is not None for tile in tiles):
        raise ValueError("canonical locality tiles cannot be passed to generic packing")
    gap = 8
    total_tile_pixels = sum(tile.pixels.shape[0] * tile.pixels.shape[1] for tile in tiles)
    target_width = max(
        max(tile.pixels.shape[1] for tile in tiles),
        math.ceil(math.sqrt(total_tile_pixels)),
    )
    mutable_rows: list[list[_Tile]] = []
    current_row: list[_Tile] = []
    current_width = 0
    for tile in tiles:
        tile_width = tile.pixels.shape[1]
        projected_width = current_width + gap + tile_width if current_row else tile_width
        if current_row and projected_width > target_width:
            mutable_rows.append(current_row)
            current_row = []
            current_width = 0
        current_row.append(tile)
        current_width = current_width + gap + tile_width if len(current_row) > 1 else tile_width
    if current_row:
        mutable_rows.append(current_row)
    rows = tuple(tuple(row) for row in mutable_rows)
    row_heights = tuple(max(tile.pixels.shape[0] for tile in row) for row in rows)
    row_tops = []
    top = 0
    for height in row_heights:
        row_tops.append(top)
        top += height + gap
    row_widths = tuple(sum(tile.pixels.shape[1] for tile in row) + gap * (len(row) - 1) for row in rows)
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
    median_text_height = sorted(text_line_heights)[len(text_line_heights) // 2] if text_line_heights else 24
    requested_scale = min(
        4,
        max(1, math.ceil(24 / max(1, median_text_height))),
    )
    pixel_limited_scale = max(
        1,
        math.floor(math.sqrt(16_000_000 / max(1, atlas_width * atlas_height))),
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


@dataclass(frozen=True)
class _LocalRegionLayout:
    matrix_segment_shape: tuple[int, int]
    column_widths: tuple[int, ...]
    row_heights: tuple[int, ...]


_LocalitySlotKey = tuple[
    str,
    tuple[str, ...],
    tuple[int, int, int, int],
]


def _locality_slot_key(tile: _Tile) -> _LocalitySlotKey:
    return (
        tile.unit_id,
        tile.segment_ids,
        tile.source_bbox.as_tuple(),
    )


def _slot_for_locality_tile(
    slots: dict[str | _LocalitySlotKey, int],
    tile: _Tile,
) -> int:
    key = _locality_slot_key(tile)
    if key in slots:
        return slots[key]
    try:
        return slots[tile.unit_id]
    except KeyError as error:
        raise ValueError("local tile is outside its locality-family contract") from error


def _bounded_locality_family_slots(
    tiles: tuple[_Tile, ...],
    block: RecognitionBlock,
) -> tuple[
    dict[str | _LocalitySlotKey, int],
    tuple[int, int],
]:
    """Assign only occupied block-local tiles to a bounded 16x16 canvas."""

    if not tiles:
        raise ValueError("locality-family contract requires occupied tiles")
    if len(tiles) > 256:
        raise BlockPlanningLimitError(
            "locality-family block exceeds 256 occupied tiles; " "split it dyadically before rendering"
        )
    metadata_by_segment = {placement.segment_id: placement for placement in block.local_placements}
    tiles_by_island: dict[str, list[tuple[int, _Tile]]] = {island.island_id: [] for island in block.local_islands}
    for tile in tiles:
        metadata = tuple(metadata_by_segment.get(segment_id) for segment_id in tile.segment_ids)
        if not metadata or any(item is None for item in metadata):
            raise ValueError("local tile lacks planner placement metadata")
        positions = {(item.island_id, item.polar_order) for item in metadata if item is not None}
        if len(positions) != 1:
            raise ValueError("one local tile cannot span multiple planner positions")
        island_id, polar_order = next(iter(positions))
        if island_id not in tiles_by_island:
            raise ValueError("local tile references an unknown visual island")
        tiles_by_island[island_id].append((polar_order, tile))

    maximum_island_tiles = max(map(len, tiles_by_island.values()))
    columns = min(16, max(1, math.ceil(math.sqrt(maximum_island_tiles))))
    rows = math.ceil(maximum_island_tiles / columns)
    if rows > 16:
        raise BlockPlanningLimitError("locality-family occupied slots exceed the 16x16 contract")

    slots: dict[str | _LocalitySlotKey, int] = {}
    for island in block.local_islands:
        ordered = sorted(
            tiles_by_island[island.island_id],
            key=lambda item: (
                item[0],
                item[1].unit_id,
                item[1].segment_ids,
                item[1].source_bbox.as_tuple(),
            ),
        )
        for slot, (_polar_order, tile) in enumerate(ordered):
            key = _locality_slot_key(tile)
            if key in slots:
                raise ValueError("locality-family tile identity is duplicated")
            slots[key] = slot
    if len(slots) != len(tiles):
        raise ValueError("locality-family contract lost an occupied tile")
    return slots, (columns, rows)


def _derive_local_region_layouts(
    tiles: tuple[_Tile, ...],
    block: RecognitionBlock,
    *,
    membership_slot_by_id: dict[str | _LocalitySlotKey, int] | None = None,
    slot_shape: tuple[int, int] | None = None,
) -> dict[str, _LocalRegionLayout]:
    metadata_by_segment = {placement.segment_id: placement for placement in block.local_placements}
    tiles_by_region: dict[str, list[tuple[_Tile, int, int]]] = {
        island.local_region_id: [] for island in block.local_islands
    }
    for tile in tiles:
        if len(tile.segment_ids) != 1:
            raise ValueError("one local tile must represent one planner position")
        metadata = metadata_by_segment.get(tile.segment_ids[0])
        if metadata is None or metadata.local_region_id not in tiles_by_region:
            raise ValueError("local tile lacks planner region metadata")
        if membership_slot_by_id is None:
            matrix_row = metadata.matrix_row
            matrix_column = metadata.matrix_column
        else:
            if slot_shape is None or slot_shape[0] < 1 or slot_shape[1] < 1:
                raise ValueError("canonical membership slot shape is invalid")
            slot = _slot_for_locality_tile(membership_slot_by_id, tile)
            matrix_row, matrix_column = divmod(slot, slot_shape[0])
        tiles_by_region[metadata.local_region_id].append((tile, matrix_row, matrix_column))

    layouts: dict[str, _LocalRegionLayout] = {}
    for island in block.local_islands:
        occupied = tiles_by_region[island.local_region_id]
        rows = (
            max(
                (matrix_row for _tile, matrix_row, _matrix_column in occupied),
                default=0,
            )
            + 1
        )
        columns = (
            max(
                (matrix_column for _tile, _matrix_row, matrix_column in occupied),
                default=0,
            )
            + 1
        )
        column_widths = [0] * columns
        row_heights = [0] * rows
        for tile, matrix_row, matrix_column in occupied:
            column_widths[matrix_column] = max(column_widths[matrix_column], tile.pixels.shape[1])
            row_heights[matrix_row] = max(row_heights[matrix_row], tile.pixels.shape[0])
        layouts[island.local_region_id] = _LocalRegionLayout(
            matrix_segment_shape=(rows, columns),
            column_widths=tuple(column_widths),
            row_heights=tuple(row_heights),
        )
    return layouts


def _render_canonical_locality_raster(
    tiles: tuple[_Tile, ...],
    block: RecognitionBlock,
    *,
    region_layouts: dict[str, _LocalRegionLayout] | None = None,
    membership_slot_by_id: dict[str | _LocalitySlotKey, int] | None = None,
    slot_shape: tuple[int, int] | None = None,
    maximum_pixels: int = 16_000_000,
) -> CanonicalLocalityRaster:
    """Render occupied polar-local positions without flattening their layout."""

    if not tiles:
        raise ValueError("local tile packing requires at least one real tile")
    if maximum_pixels <= 0:
        raise ValueError("local tile pixel limit must be positive")
    metadata_by_segment = {placement.segment_id: placement for placement in block.local_placements}
    tiles_by_island: dict[
        str,
        list[tuple[int, int, int, int, int, _Tile]],
    ] = {island.island_id: [] for island in block.local_islands}
    for tile in tiles:
        metadata = tuple(metadata_by_segment.get(segment_id) for segment_id in tile.segment_ids)
        if not metadata or any(item is None for item in metadata):
            raise ValueError("local tile lacks planner placement metadata")
        positions = {
            (
                item.island_id,
                item.table_row,
                item.table_column,
                item.matrix_row,
                item.matrix_column,
                item.polar_order,
            )
            for item in metadata
            if item is not None
        }
        if len(positions) != 1:
            raise ValueError("one local tile cannot span multiple planner positions")
        (
            island_id,
            table_row,
            table_column,
            matrix_row,
            matrix_column,
            polar_order,
        ) = next(iter(positions))
        if membership_slot_by_id is not None:
            if slot_shape is None or slot_shape[0] < 1 or slot_shape[1] < 1:
                raise ValueError("canonical membership slot shape is invalid")
            slot = _slot_for_locality_tile(membership_slot_by_id, tile)
            matrix_row, matrix_column = divmod(slot, slot_shape[0])
        if island_id not in tiles_by_island:
            raise ValueError("local tile references an unknown visual island")
        tiles_by_island[island_id].append(
            (
                table_row,
                table_column,
                matrix_row,
                matrix_column,
                polar_order,
                tile,
            )
        )

    gap = 8
    median_tile_height = sorted(tile.pixels.shape[0] for tile in tiles)[len(tiles) // 2]
    island_separator = max(gap * 2, median_tile_height)
    derived_layouts = _derive_local_region_layouts(
        tiles,
        block,
        membership_slot_by_id=membership_slot_by_id,
        slot_shape=slot_shape,
    )
    effective_layouts = (
        derived_layouts
        if region_layouts is None
        else {
            **derived_layouts,
            **region_layouts,
        }
    )
    island_layouts: list[tuple[tuple[tuple[_Tile, int, int], ...], int, int]] = []
    for island in block.local_islands:
        layout = effective_layouts[island.local_region_id]
        if any(
            matrix_row >= layout.matrix_segment_shape[0] or matrix_column >= layout.matrix_segment_shape[1]
            for (
                _table_row,
                _table_column,
                matrix_row,
                matrix_column,
                _polar_order,
                _tile,
            ) in tiles_by_island[island.island_id]
        ):
            raise ValueError("local region layout does not cover an occupied position")
        column_lefts: list[int] = []
        left = 0
        for column_width in layout.column_widths:
            column_lefts.append(left)
            left += column_width + gap
        island_width = max(1, left - gap)
        row_tops: list[int] = []
        top = 0
        for row_height in layout.row_heights:
            row_tops.append(top)
            top += row_height + gap
        island_height = max(1, top - gap)
        ordered = sorted(
            tiles_by_island[island.island_id],
            key=lambda item: (
                item[2],
                item[3],
                item[4],
                item[5].unit_id,
            ),
        )
        placements: list[tuple[_Tile, int, int]] = []
        for (
            _table_row,
            _table_column,
            matrix_row,
            matrix_column,
            _polar_order,
            tile,
        ) in ordered:
            tile_height, tile_width = tile.pixels.shape[:2]
            placements.append(
                (
                    tile,
                    column_lefts[matrix_column] + (layout.column_widths[matrix_column] - tile_width) // 2,
                    row_tops[matrix_row] + (layout.row_heights[matrix_row] - tile_height) // 2,
                )
            )
        if placements:
            island_layouts.append((tuple(placements), island_width, island_height))
    if not island_layouts:
        raise ValueError("local tile packing has no occupied visual island")

    atlas_width = max(layout[1] for layout in island_layouts)
    atlas_height = sum(layout[2] for layout in island_layouts) + (island_separator * (len(island_layouts) - 1))
    source_placements: list[tuple[_Tile, int, int]] = []
    island_top = 0
    for island_placements, island_width, island_height in island_layouts:
        island_left = (atlas_width - island_width) // 2
        source_placements.extend((tile, left + island_left, top + island_top) for tile, left, top in island_placements)
        island_top += island_height + island_separator
    base_pixels = atlas_width * atlas_height
    if base_pixels > maximum_pixels:
        raise BlockPlanningLimitError("local compact layout exceeds configured pixel limit " f"{maximum_pixels}")

    atlas = np.full((atlas_height, atlas_width, 3), 255, dtype=np.uint8)
    for tile, left, tile_top in source_placements:
        height, width = tile.pixels.shape[:2]
        atlas[tile_top : tile_top + height, left : left + width] = tile.pixels

    text_line_heights: list[int] = []
    for tile in tiles:
        ink = np.any(tile.pixels < 248, axis=2)
        occupied_rows = np.flatnonzero(np.any(ink, axis=1))
        if not occupied_rows.size:
            continue
        run_start = int(occupied_rows[0])
        previous = run_start
        for row in map(int, occupied_rows[1:]):
            if row > previous + 1:
                if previous - run_start + 1 >= 2:
                    text_line_heights.append(previous - run_start + 1)
                run_start = row
            previous = row
        if previous - run_start + 1 >= 2:
            text_line_heights.append(previous - run_start + 1)
    median_text_height = sorted(text_line_heights)[len(text_line_heights) // 2] if text_line_heights else 24
    requested_scale = min(
        4,
        max(1, math.ceil(24 / max(1, median_text_height))),
    )
    pixel_limited_scale = math.floor(math.sqrt(maximum_pixels / max(1, base_pixels)))
    if pixel_limited_scale < 1:
        raise BlockPlanningLimitError("local compact layout cannot fit without shrinking tiles")
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

    placements = tuple(
        CompactionPlacement(
            unit_id=tile.unit_id,
            segment_ids=tile.segment_ids,
            source_bbox=tile.source_bbox,
            crop_bbox=Box(
                left * scale,
                tile_top * scale,
                (left + tile.pixels.shape[1]) * scale,
                (tile_top + tile.pixels.shape[0]) * scale,
            ),
        )
        for tile, left, tile_top in source_placements
    )
    return CanonicalLocalityRaster(
        png_bytes=payload,
        placements=placements,
        sha256=hashlib.sha256(payload).hexdigest(),
        packed_canvas_pixels=atlas_width * atlas_height * scale * scale,
        size=(atlas_width * scale, atlas_height * scale),
    )


def _pack_local_tiles(
    tiles: tuple[_Tile, ...],
    block: RecognitionBlock,
    *,
    region_layouts: dict[str, _LocalRegionLayout] | None = None,
    maximum_pixels: int = 16_000_000,
) -> tuple[
    bytes,
    tuple[CompactionPlacement, ...],
    int,
    tuple[int, int],
]:
    """Compatibility view over the single canonical locality renderer."""

    rendered = _render_canonical_locality_raster(
        tiles,
        block,
        region_layouts=region_layouts,
        maximum_pixels=maximum_pixels,
    )
    return (
        rendered.png_bytes,
        rendered.placements,
        rendered.packed_canvas_pixels,
        rendered.size,
    )


def _crop_canonical_locality_subset(
    tiles: tuple[_Tile, ...],
) -> CanonicalLocalityRaster:
    """Crop a contextual subset without changing canonical local layout."""

    if not tiles or any(tile.canonical_bbox is None for tile in tiles):
        raise ValueError("canonical locality subset requires canonical boxes")
    source_payloads = {tile.canonical_png_bytes for tile in tiles}
    if None in source_payloads or len(source_payloads) != 1:
        raise ValueError("canonical locality subset requires one immutable source raster")
    source_payload = next(iter(source_payloads))
    assert source_payload is not None
    bounds = Box.union(tile.canonical_bbox for tile in tiles if tile.canonical_bbox is not None)
    placements = []
    for tile in tiles:
        canonical_bbox = tile.canonical_bbox
        assert canonical_bbox is not None
        expected_size = (canonical_bbox.height, canonical_bbox.width)
        if tile.pixels.shape[:2] != expected_size:
            raise ValueError("canonical locality tile geometry disagrees with placement")
        left = canonical_bbox.left - bounds.left
        top = canonical_bbox.top - bounds.top
        placements.append(
            CompactionPlacement(
                unit_id=tile.unit_id,
                segment_ids=tile.segment_ids,
                source_bbox=tile.source_bbox,
                crop_bbox=Box(
                    left,
                    top,
                    left + canonical_bbox.width,
                    top + canonical_bbox.height,
                ),
            )
        )
    with Image.open(io.BytesIO(source_payload)) as opened:
        opened.load()
        if bounds.left < 0 or bounds.top < 0 or bounds.right > opened.width or bounds.bottom > opened.height:
            raise ValueError("canonical subset exceeds source raster")
        subset = opened.convert("RGB").crop(bounds.as_tuple())
    try:
        payload = _png_bytes(np.asarray(subset).copy())
    finally:
        subset.close()
    return CanonicalLocalityRaster(
        png_bytes=payload,
        placements=tuple(placements),
        sha256=hashlib.sha256(payload).hexdigest(),
        packed_canvas_pixels=bounds.width * bounds.height,
        size=(bounds.width, bounds.height),
    )


def _source_placement_artifacts(
    tiles: tuple[_Tile, ...],
    block: RecognitionBlock,
    page: np.ndarray,
) -> tuple[SourcePlacementArtifact, ...]:
    metadata_by_segment = {placement.segment_id: placement for placement in block.local_placements}
    spatial_positions: dict[str, tuple[str, int, int, int]] = {}
    if not metadata_by_segment:
        widths = sorted(tile.source_bbox.width for tile in tiles)
        median_width = widths[len(widths) // 2]

        def band_indexes(
            members: tuple[_Tile, ...],
            *,
            vertical: bool,
        ) -> dict[str, int]:
            bands: list[tuple[int, int]] = []
            result: dict[str, int] = {}
            ordered = sorted(
                members,
                key=lambda tile: (
                    tile.source_bbox.top if vertical else tile.source_bbox.left,
                    tile.source_bbox.left if vertical else tile.source_bbox.top,
                    tile.unit_id,
                ),
            )
            for tile in ordered:
                start = tile.source_bbox.top if vertical else tile.source_bbox.left
                stop = tile.source_bbox.bottom if vertical else tile.source_bbox.right
                best_index = None
                best_overlap = 0
                for index, (band_start, band_stop) in enumerate(bands):
                    overlap = max(
                        0,
                        min(stop, band_stop) - max(start, band_start),
                    )
                    if overlap > best_overlap and overlap * 2 >= min(
                        stop - start,
                        band_stop - band_start,
                    ):
                        best_index = index
                        best_overlap = overlap
                if best_index is None:
                    best_index = len(bands)
                    bands.append((start, stop))
                else:
                    band_start, band_stop = bands[best_index]
                    bands[best_index] = (
                        min(start, band_start),
                        max(stop, band_stop),
                    )
                result[tile.unit_id] = best_index
            return result

        row_by_id = band_indexes(tiles, vertical=True)
        column_members = tuple(tile for tile in tiles if tile.source_bbox.width <= max(1, median_width * 3))
        column_by_id = band_indexes(column_members, vertical=False)
        reading_order = {
            tile.unit_id: index
            for index, tile in enumerate(
                sorted(
                    tiles,
                    key=lambda item: (
                        row_by_id[item.unit_id],
                        column_by_id.get(item.unit_id, 1_000_000),
                        item.source_bbox.top,
                        item.source_bbox.left,
                        item.unit_id,
                    ),
                )
            )
        }
        for tile in tiles:
            spatial_positions[tile.unit_id] = (
                f"spatial:{block.block_id}",
                row_by_id[tile.unit_id],
                column_by_id.get(tile.unit_id, -(reading_order[tile.unit_id] + 1)),
                reading_order[tile.unit_id],
            )
    artifacts = []
    page_height, page_width = page.shape[:2]
    for tile in tiles:
        metadata = tuple(metadata_by_segment.get(segment_id) for segment_id in tile.segment_ids)
        positions = (
            {
                (
                    item.island_id,
                    item.matrix_row,
                    item.matrix_column,
                    item.polar_order,
                )
                for item in metadata
                if item is not None
            }
            if metadata_by_segment
            else {spatial_positions[tile.unit_id]}
        )
        if metadata_by_segment and (not metadata or any(item is None for item in metadata)):
            raise ValueError("source placement lacks planner metadata")
        if len(positions) != 1:
            raise ValueError("source placement spans multiple polar positions")
        bbox = tile.source_bbox
        if (
            bbox.left < 0
            or bbox.top < 0
            or bbox.right > page_width
            or bbox.bottom > page_height
            or bbox.width <= 0
            or bbox.height <= 0
        ):
            raise ValueError("source placement bbox exceeds aligned page")
        source_pixels = page[
            bbox.top : bbox.bottom,
            bbox.left : bbox.right,
        ].copy()
        payload = _png_bytes(source_pixels)
        island_id, matrix_row, matrix_column, polar_order = next(iter(positions))
        artifacts.append(
            SourcePlacementArtifact(
                unit_id=tile.unit_id,
                segment_ids=tile.segment_ids,
                source_bbox=bbox,
                png_bytes=payload,
                sha256=hashlib.sha256(payload).hexdigest(),
                island_id=island_id,
                matrix_row=matrix_row,
                matrix_column=matrix_column,
                polar_order=polar_order,
            )
        )
    return tuple(artifacts)


def build_deferred_compact_crops(
    page_png: bytes,
    *,
    aligned_size: tuple[int, int],
    plan: BlockPlan,
    ownership: np.ndarray | None,
    ownership_segment_ids: tuple[str, ...] | None,
    segment_spans: tuple[SegmentSpan, ...] = (),
    segment_bboxes: dict[str, Box] | None = None,
) -> tuple[tuple[BlockCropPair, ...], tuple[BlockCompaction, ...]]:
    """Build raw compact crops; enhancement is deferred to each OCR attempt."""

    with Image.open(io.BytesIO(page_png)) as opened:
        opened.load()
        if opened.format != "PNG" or opened.size != aligned_size:
            raise ValueError("deferred crop source disagrees with aligned geometry")
        page = np.asarray(opened.convert("RGB")).copy()
    label_by_segment = {segment_id: label for label, segment_id in enumerate(ownership_segment_ids or ())}
    span_by_segment = {span.segment_id: span for span in segment_spans}
    outputs: list[BlockCropPair] = []
    compacted: list[BlockCompaction] = []
    unit_by_segment = {segment_id: unit for unit in plan.membership_units for segment_id in unit.segment_ids}
    tile_cache: dict[tuple[str, ...], _Tile | None] = {}
    for block in plan.blocks:
        local_layout = block.matrix_window_kind in {
            "polar-local-full",
            "polar-local-signature",
        }
        if local_layout and (ownership is None or segment_bboxes is None):
            raise ValueError("polar-local compaction requires ownership and segment bboxes")
        bbox = block.bbox
        crop_bbox = bbox
        pixels = page[bbox.top : bbox.bottom, bbox.left : bbox.right].copy()
        region_ownership = ownership[bbox.top : bbox.bottom, bbox.left : bbox.right] if ownership is not None else None
        selected_labels = {
            label_by_segment[segment_id] for segment_id in block.segment_ids if segment_id in label_by_segment
        }
        if region_ownership is not None:
            foreign_labels = set(
                int(value)
                for value in np.unique(region_ownership)
                if int(value) >= 0 and int(value) not in selected_labels
            )
            if foreign_labels:
                pixels[np.isin(region_ownership, tuple(foreign_labels))] = 255

        tiles: list[_Tile] = []
        omitted: list[str] = []
        if region_ownership is not None:
            grouped_segment_ids: dict[
                tuple[int, int, int, int, str],
                list[str],
            ] = {}
            for segment_id in block.segment_ids:
                span = span_by_segment.get(segment_id)
                key = (
                    (0, 0, 0, 0, segment_id)
                    if local_layout
                    else (
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
                )
                grouped_segment_ids.setdefault(key, []).append(segment_id)
            for group_ids_list in grouped_segment_ids.values():
                group_ids = tuple(group_ids_list)
                unit = unit_by_segment.get(group_ids[0])
                labels = tuple(
                    label_by_segment[segment_id] for segment_id in group_ids if segment_id in label_by_segment
                )
                if unit is None or not labels:
                    if unit is not None:
                        omitted.append(unit.unit_id)
                    continue
                if group_ids in tile_cache:
                    cached = tile_cache[group_ids]
                    if cached is None:
                        omitted.append(unit.unit_id)
                    else:
                        tiles.append(cached)
                    continue
                if segment_bboxes is not None and all(segment_id in segment_bboxes for segment_id in group_ids):
                    member_bbox = Box.union(segment_bboxes[segment_id] for segment_id in group_ids)
                    analysis_bbox = Box(
                        max(0, member_bbox.left - 4),
                        max(0, member_bbox.top - 4),
                        min(aligned_size[0], member_bbox.right + 4),
                        min(aligned_size[1], member_bbox.bottom + 4),
                    )
                    unit_pixels = page[
                        analysis_bbox.top : analysis_bbox.bottom,
                        analysis_bbox.left : analysis_bbox.right,
                    ].copy()
                    unit_ownership = ownership[
                        analysis_bbox.top : analysis_bbox.bottom,
                        analysis_bbox.left : analysis_bbox.right,
                    ]
                    unit_mask = np.isin(unit_ownership, labels)
                    ink_mask, dark_on_light = _content_ink_mask(
                        unit_pixels,
                        unit_mask,
                    )
                    rows, columns = np.nonzero(ink_mask)
                    if not len(rows):
                        tile_cache[group_ids] = None
                        omitted.append(unit.unit_id)
                        continue
                    local_left = max(0, int(columns.min()) - 2)
                    local_top = max(0, int(rows.min()) - 2)
                    local_right = min(unit_pixels.shape[1], int(columns.max()) + 3)
                    local_bottom = min(unit_pixels.shape[0], int(rows.max()) + 3)
                    source_bbox = Box(
                        analysis_bbox.left + local_left,
                        analysis_bbox.top + local_top,
                        analysis_bbox.left + local_right,
                        analysis_bbox.top + local_bottom,
                    )
                    tile_pixels = unit_pixels[
                        local_top:local_bottom,
                        local_left:local_right,
                    ].copy()
                    local_ink_mask = ink_mask[
                        local_top:local_bottom,
                        local_left:local_right,
                    ]
                    if local_layout:
                        exact_bbox = segment_bboxes[group_ids[0]]
                        local_left = exact_bbox.left - analysis_bbox.left
                        local_top = exact_bbox.top - analysis_bbox.top
                        local_right = exact_bbox.right - analysis_bbox.left
                        local_bottom = exact_bbox.bottom - analysis_bbox.top
                        source_bbox = exact_bbox
                        tile_pixels = unit_pixels[
                            local_top:local_bottom,
                            local_left:local_right,
                        ].copy()
                        local_ink_mask = ink_mask[
                            local_top:local_bottom,
                            local_left:local_right,
                        ]
                        if not np.any(local_ink_mask):
                            tile_cache[group_ids] = None
                            omitted.append(unit.unit_id)
                            continue
                    retained_mask = _dilate_mask(local_ink_mask)
                    if dark_on_light:
                        if not local_layout:
                            tile_pixels[~retained_mask] = 255
                    else:
                        local_luminance = np.rint(
                            tile_pixels[:, :, 0].astype(np.float32) * 0.299
                            + tile_pixels[:, :, 1].astype(np.float32) * 0.587
                            + tile_pixels[:, :, 2].astype(np.float32) * 0.114
                        ).astype(np.uint8)
                        normalized = np.full_like(tile_pixels, 255)
                        inverted = 255 - local_luminance
                        normalized[retained_mask] = np.repeat(
                            inverted[:, :, None],
                            3,
                            axis=2,
                        )[retained_mask]
                        tile_pixels = normalized
                    if not local_layout:
                        tile_pixels = _compact_internal_whitespace(
                            tile_pixels,
                            local_ink_mask,
                        )
                    tile = _Tile(
                        unit_id=unit.unit_id,
                        segment_ids=group_ids,
                        source_bbox=source_bbox,
                        pixels=tile_pixels,
                    )
                    tile_cache[group_ids] = tile
                    tiles.append(tile)
                    continue
                unit_mask = np.isin(region_ownership, labels)
                ink_mask, dark_on_light = _content_ink_mask(
                    pixels,
                    unit_mask,
                )
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
                local_ink_mask = ink_mask[
                    local_top:local_bottom,
                    local_left:local_right,
                ]
                retained_mask = _dilate_mask(local_ink_mask)
                if dark_on_light:
                    tile_pixels[~retained_mask] = 255
                else:
                    local_luminance = np.rint(
                        tile_pixels[:, :, 0].astype(np.float32) * 0.299
                        + tile_pixels[:, :, 1].astype(np.float32) * 0.587
                        + tile_pixels[:, :, 2].astype(np.float32) * 0.114
                    ).astype(np.uint8)
                    normalized = np.full_like(tile_pixels, 255)
                    inverted = 255 - local_luminance
                    normalized[retained_mask] = np.repeat(
                        inverted[:, :, None],
                        3,
                        axis=2,
                    )[retained_mask]
                    tile_pixels = normalized
                tile = _Tile(
                    unit_id=unit.unit_id,
                    segment_ids=group_ids,
                    source_bbox=source_bbox,
                    pixels=_compact_internal_whitespace(
                        tile_pixels,
                        local_ink_mask,
                    ),
                )
                tile_cache[group_ids] = tile
                tiles.append(tile)

        if tiles:
            source_placements: tuple[SourcePlacementArtifact, ...] = ()
            if local_layout:
                membership_slot_by_id, slot_shape = _bounded_locality_family_slots(tuple(tiles), block)
                rendered = _render_canonical_locality_raster(
                    tuple(tiles),
                    block,
                    membership_slot_by_id=membership_slot_by_id,
                    slot_shape=slot_shape,
                )
                payload = rendered.png_bytes
                placements = rendered.placements
                packed_pixels = rendered.packed_canvas_pixels
                packed_size = rendered.size
                raster_kind = CompactionRasterKind.CANONICAL_LOCALITY
            else:
                payload, placements, packed_pixels, packed_size = _pack_tiles(tuple(tiles))
                raster_kind = CompactionRasterKind.SPATIAL_PACKED
            source_placements = _source_placement_artifacts(
                tuple(tiles),
                block,
                page,
            )
            crop_bbox = Box(0, 0, packed_size[0], packed_size[1])
            occupied_before = sum(tile.pixels.shape[0] * tile.pixels.shape[1] for tile in tiles)
            compacted.append(
                BlockCompaction(
                    block_id=block.block_id,
                    placements=placements,
                    omitted_empty_units=tuple(dict.fromkeys(omitted)),
                    occupied_pixels_before=occupied_before,
                    occupied_pixels_after=sum(
                        (placement.crop_bbox.right - placement.crop_bbox.left)
                        * (placement.crop_bbox.bottom - placement.crop_bbox.top)
                        for placement in placements
                    ),
                    packed_canvas_pixels=packed_pixels,
                    raster_kind=raster_kind,
                    raw_sha256=hashlib.sha256(payload).hexdigest(),
                    source_placements=source_placements,
                )
            )
        else:
            payload = _png_bytes(pixels)
            block_units = tuple(
                unit
                for unit in plan.membership_units
                if block.block_id in unit.block_ids and set(unit.segment_ids).issubset(block.segment_ids)
            )
            if len(block_units) == 1:
                unit = block_units[0]
                payload_sha256 = hashlib.sha256(payload).hexdigest()
                placement = CompactionPlacement(
                    unit_id=unit.unit_id,
                    segment_ids=unit.segment_ids,
                    source_bbox=block.bbox,
                    crop_bbox=Box(0, 0, block.bbox.width, block.bbox.height),
                )
                source_placement = SourcePlacementArtifact(
                    unit_id=unit.unit_id,
                    segment_ids=unit.segment_ids,
                    source_bbox=block.bbox,
                    png_bytes=payload,
                    sha256=payload_sha256,
                    island_id=f"spatial:{block.block_id}",
                    matrix_row=0,
                    matrix_column=0,
                    polar_order=0,
                )
                compacted.append(
                    BlockCompaction(
                        block_id=block.block_id,
                        placements=(placement,),
                        omitted_empty_units=(),
                        occupied_pixels_before=block.bbox.area,
                        occupied_pixels_after=block.bbox.area,
                        packed_canvas_pixels=block.bbox.area,
                        raster_kind=CompactionRasterKind.SPATIAL_PACKED,
                        raw_sha256=payload_sha256,
                        source_placements=(source_placement,),
                    )
                )
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
_VOWELS = frozenset("aeiouyAEIOUY" "аеёиоуыэюяАЕЁИОУЫЭЮЯ" "αεηιουωΑΕΗΙΟΥΩ")
_QUOTE_ADJACENT_PIPE = re.compile(r"[\"'“”‘’]\|(?=\s)")
_NUMERIC_SEPARATOR = re.compile(r"\d\s*[/:-]\s*\d")


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


def _text_selection_evidence(text: str) -> tuple[int, int, int]:
    script_counts: dict[str, int] = {}
    for character in text:
        script = _character_script(character)
        if script != "neutral":
            script_counts[script] = script_counts.get(script, 0) + 1
    total_letters = sum(script_counts.values())
    dominant_script: str | None = None
    if total_letters >= 8 and script_counts:
        script, count = max(
            script_counts.items(),
            key=lambda item: (item[1], item[0]),
        )
        if count / total_letters >= 0.85:
            dominant_script = script

    minority_confusables = 0
    if dominant_script is not None:
        for token in _TOKEN.findall(text):
            letters = tuple(character for character in token if character.isalpha())
            token_scripts = {_character_script(character) for character in letters} - {"neutral"}
            minority_scripts = token_scripts - {dominant_script}
            if minority_scripts and (len(letters) <= 3 or len(token_scripts) > 1):
                minority_confusables += 1

    malformed_punctuation = len(_QUOTE_ADJACENT_PIPE.findall(text))
    numeric_separators = len(_NUMERIC_SEPARATOR.findall(text))
    return minority_confusables, malformed_punctuation, numeric_separators


def assess_grammar(
    output: OcrEngineOutput,
    languages: tuple[str, ...],
) -> GrammarAssessment:
    text = output.text.strip()
    if not text:
        return GrammarAssessment(0, False, ("empty",))
    confidences = tuple(word.confidence for word in output.words)
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    minimum_confidence = min(confidences) if confidences else 0.0
    letters = tuple(character for character in text if character.isalpha())
    allowed = _allowed_scripts(languages)
    matching_letters = sum(_character_script(character) in allowed for character in letters)
    script_fit = matching_letters / len(letters) if letters else 1.0
    controls = sum(unicodedata.category(character).startswith("C") and not character.isspace() for character in text)
    visible = tuple(character for character in text if not character.isspace())
    symbols = sum(not character.isalnum() and character not in ".,:;!?%+-=*/()[]{}<>_|#@" for character in visible)
    symbol_ratio = symbols / len(visible) if visible else 1.0
    tokens = tuple(_TOKEN.findall(text))
    lexical = tuple(token for token in tokens if any(char.isalpha() for char in token))
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
    semantic_shape = bool(letters) or len(tokens) > 1 or any(character in "%=+-*/" for character in text)
    cyrillic_letters = sum(1 for character in text if "\u0400" <= character <= "\u04ff")
    latin_letters = sum(1 for character in text if "a" <= character.lower() <= "z")
    allowed_latin_tokens = {"CI", "PR", "README", "SCA", "SBOM"}
    suspicious_latin_tokens = {
        token
        for token in re.findall(r"[A-Za-z]+", text)
        if token.upper() not in allowed_latin_tokens
        and (len(token) <= 2 or (not token.islower() and not token.isupper()))
    }
    suspicious_script_mix = cyrillic_letters > latin_letters * 2 and bool(suspicious_latin_tokens)
    (
        minority_confusables,
        malformed_punctuation,
        numeric_separators,
    ) = _text_selection_evidence(text)
    singletons = sum(len(token) == 1 and token.isalpha() for token in lexical)
    singleton_ratio = singletons / len(lexical) if lexical else 0.0
    score = (
        mean_confidence * 0.48 + script_fit * 0.28 + lexical_fit * 0.18 + (1.0 - min(1.0, symbol_ratio * 3.0)) * 0.06
    )
    score -= min(0.35, singleton_ratio * 0.25)
    score -= min(0.50, controls * 0.20)
    score -= min(0.12, minority_confusables * 0.04)
    score -= min(0.09, malformed_punctuation * 0.03)
    score += min(0.02, numeric_separators * 0.01)
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
        and minority_confusables == 0
        and malformed_punctuation == 0
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
    if minority_confusables:
        reasons.append("dominant-script-confusable")
    if malformed_punctuation:
        reasons.append("punctuation-confusable")
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
    cache_hit: bool = False


@dataclass(frozen=True)
class _NativeScriptRun:
    script_kind: str
    text: str
    confidence: float


@dataclass(frozen=True)
class _RecursiveNativeRun:
    script_kind: str
    start_index: int
    stop_index: int
    text: str
    confidence: float
    bbox: Box


@dataclass(frozen=True)
class _RecognitionDecision:
    candidate: _Candidate | None
    profile_candidates: tuple[_Candidate, ...]
    primary_grammar_percent: int
    all_profiles_below_lock: bool
    primary_raw_candidate: _Candidate | None = None


@dataclass(frozen=True)
class _IsolatedRecognition:
    candidate: _Candidate | None
    profile_candidates: tuple[_Candidate, ...]
    attempts: tuple[LanguageAttempt, ...]
    observations: tuple[tuple[str, int], ...]
    elapsed_seconds: float
    all_profiles_below_lock: bool
    primary_raw_candidate: _Candidate | None = None


@dataclass(frozen=True)
class _IsolatedRecursiveRecognition:
    candidate: _Candidate | None
    attempts: tuple[LanguageAttempt, ...]
    observations: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class _FullBlockRecognition:
    candidate: _Candidate | None
    elapsed_seconds: float
    all_profiles_below_lock: bool
    fallback_profile_ids: tuple[str, ...]
    native_script_runs: tuple[_NativeScriptRun, ...]
    native_script_unit_ids: tuple[str, ...] = ()
    primary_raw_candidate: _Candidate | None = None


@dataclass(frozen=True)
class _CachedUnitRecognition:
    candidate: _Candidate | None
    crop_size: tuple[int, int]


@dataclass(frozen=True)
class _ContextGroup:
    group_id: str
    depth: int
    first_order: int
    tiles: tuple[_Tile, ...]
    png_bytes: bytes
    placements: tuple[CompactionPlacement, ...]
    emit_unit_ids: tuple[str, ...] = ()
    source_fallback: bool = False


@dataclass(frozen=True)
class _CachedGroupRecognition:
    candidate: _Candidate | None


class _SourceLineCandidateCache:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._entries: dict[tuple[object, ...], _Candidate | None] = {}
        self._resolved: set[tuple[object, ...]] = set()
        self._inflight: set[tuple[object, ...]] = set()

    def resolve(
        self,
        key: tuple[object, ...],
        operation: Callable[[], _Candidate | None],
    ) -> _Candidate | None:
        with self._condition:
            while True:
                if key in self._resolved:
                    return self._entries[key]
                if key not in self._inflight:
                    self._inflight.add(key)
                    break
                self._condition.wait()
        try:
            candidate = operation()
        except Exception:
            with self._condition:
                self._inflight.remove(key)
                self._condition.notify_all()
            raise
        with self._condition:
            self._entries[key] = candidate
            self._resolved.add(key)
            self._inflight.remove(key)
            self._condition.notify_all()
        return candidate


class AdaptivePersistentOcrSession:
    """Sequential language scheduler with bounded membership recursion."""

    _LOCK_GRAMMAR_PERCENT = 97
    _DEFAULT_PROFILE_ID = "rus-eng"
    _SPECIALIZED_MIN_MEAN_CONFIDENCE = 0.60
    _SPECIALIZED_MIN_SCRIPT_DENSITY = 0.03
    _LOCAL_NATIVE_MIN_SCRIPT_DENSITY = 0.50
    _LOCAL_NATIVE_MIN_CHARACTERS = 2
    _CJK_PROFILE_ALIASES = frozenset(("chi_sim", "chi_tra", "hans", "hant"))
    _GREEK_PROFILE_ALIASES = frozenset(("ell", "greek"))
    _MATH_PROFILE_ALIASES = frozenset(("equ",))
    _NATIVE_SCRIPT_EVIDENCE_KINDS = frozenset(("cjk", "greek"))
    _FEATURE_EVIDENCE_KINDS = frozenset(("math",))
    _SPLIT_CALIBRATION_SAMPLES = 3
    _SPLIT_CALIBRATION_MIN_SUCCESSES = 2
    _LANGUAGE_STRATEGY_MAXIMUM_LOSSES = 3

    def __init__(
        self,
        lanes: tuple[OcrLane, ...],
        *,
        state: LanguageSplayState | None = None,
        log_path: Path | None = None,
        content_cache: AdaptiveOcrContentCache | None = None,
    ) -> None:
        if len(lanes) != 1:
            raise ValueError("adaptive OCR requires exactly one base lane")
        self._profiles = self._profiles_from_lane(lanes[0])
        self._engine_variants = self._engine_variants_from_profiles(self._profiles)
        self._max_workers = lanes[0].max_workers
        self.state = state or LanguageSplayState(self._profiles)
        self.state.ensure_profiles(self._profiles)
        for profile_id, variants in self._engine_variants.items():
            self.state.ensure_engine_orders(
                profile_id,
                tuple(variant.languages for variant in variants),
            )
        self.log_path = log_path
        if self.log_path is not None:
            self.state.enable_attempt_input_artifacts()
        self._workers: dict[tuple[str, tuple[str, ...]], object] = {}
        self._worker_errors: dict[tuple[str, tuple[str, ...]], Exception] = {}
        self._content_cache = content_cache or AdaptiveOcrContentCache()
        self._unit_candidate_cache: dict[
            tuple[
                str,
                tuple[str, ...],
                tuple[int, int, int, int],
                str | None,
                tuple[str, ...],
            ],
            _CachedUnitRecognition,
        ] = {}
        self._group_candidate_cache: dict[
            tuple[
                tuple[str, ...],
                str | None,
                tuple[str, ...],
                OcrTransform,
            ],
            _CachedGroupRecognition,
        ] = {}
        self._source_line_candidate_cache = _SourceLineCandidateCache()
        self._enhancer = GammaDarkCropEnhancer()
        self._enhancer_id = (
            f"{type(self._enhancer).__module__}."
            f"{type(self._enhancer).__qualname__}:"
            f"{_cache_fingerprint(vars(self._enhancer))}"
        )
        self._closed = False

    @staticmethod
    def _profiles_from_lane(lane: OcrLane) -> tuple[LanguageProfile, ...]:
        prototype = lane.worker_factory()
        try:
            config = getattr(prototype, "config", None)
            if config is None:
                config = getattr(prototype, "_config", None)
            if config is None or not hasattr(config, "languages"):
                raise TypeError("adaptive OCR worker must expose a dataclass language config")
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
            candidate = tuple(item for item in str(language).split("+") if item)
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
                    adapter_id=(f"{adapter_type.__module__}." f"{adapter_type.__qualname__}"),
                    config_sha256=_cache_fingerprint(profile_config),
                    psm=str(getattr(profile_config, "psm", "")),
                )
            )
        return tuple(profiles)

    @staticmethod
    def _engine_variants_from_profiles(
        profiles: tuple[LanguageProfile, ...],
    ) -> dict[str, tuple[LanguageProfile, ...]]:
        variants: dict[str, tuple[LanguageProfile, ...]] = {}
        for profile in profiles:
            profile_variants = [profile]
            if profile.profile_id == "rus-eng" and set(profile.languages) == {"rus", "eng"}:
                reversed_languages = tuple(reversed(profile.languages))

                def reversed_factory(
                    base_factory: Callable[[], object] = (profile.worker_factory),
                    languages: tuple[str, ...] = reversed_languages,
                ) -> object:
                    prototype = base_factory()
                    try:
                        config = getattr(prototype, "config", None)
                        if config is None:
                            config = getattr(prototype, "_config", None)
                        if config is None or not hasattr(config, "languages"):
                            raise TypeError("adaptive OCR worker must expose languages")
                        adapter_type = type(prototype)
                        variant_config = replace(
                            config,
                            languages=languages,
                        )
                    finally:
                        close = getattr(prototype, "close", None)
                        if callable(close):
                            close()
                    return adapter_type(config=variant_config)

                profile_variants.insert(
                    0,
                    replace(
                        profile,
                        languages=reversed_languages,
                        worker_factory=reversed_factory,
                        config_sha256=_cache_fingerprint(
                            (
                                profile.config_sha256,
                                reversed_languages,
                            )
                        ),
                    ),
                )
            variants[profile.profile_id] = tuple(profile_variants)
        return variants

    def cache_metrics(self) -> dict[str, int | float]:
        return self._content_cache.snapshot().as_dict()

    def __enter__(self) -> AdaptivePersistentOcrSession:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _worker(self, profile: LanguageProfile) -> object:
        worker_key = (profile.profile_id, profile.languages)
        if worker_key in self._worker_errors:
            raise self._worker_errors[worker_key]
        worker = self._workers.get(worker_key)
        if worker is not None:
            return worker
        try:
            worker = profile.worker_factory()
        except Exception as exc:
            self._worker_errors[worker_key] = exc
            raise
        if not callable(getattr(worker, "recognize", None)):
            error = TypeError("language worker has no recognize()")
            self._worker_errors[worker_key] = error
            raise error
        self._workers[worker_key] = worker
        return worker

    @staticmethod
    def _mean_confidence(output: OcrEngineOutput) -> float:
        return sum(word.confidence for word in output.words) / len(output.words) if output.words else 0.0

    def _candidate_score(
        self,
        candidate: _Candidate,
    ) -> tuple[int, int, int, int, int, int, int, float, int]:
        confidence = self._mean_confidence(candidate.output)
        (
            minority_confusables,
            malformed_punctuation,
            numeric_separators,
        ) = _text_selection_evidence(candidate.output.text)
        profile_rank = next(
            index for index, profile in enumerate(self._profiles) if profile.profile_id == candidate.profile.profile_id
        )
        selection_percent = (
            candidate.assessment.percent
            - minority_confusables * 4
            - malformed_punctuation * 3
            + min(2, numeric_separators)
        )
        return (
            selection_percent,
            -minority_confusables,
            -malformed_punctuation,
            min(2, numeric_separators),
            candidate.assessment.percent,
            round(confidence * 100),
            -profile_rank,
            confidence,
            len(candidate.output.text),
        )

    def _candidate_is_supported_homogeneous(
        self,
        candidate: _Candidate | None,
    ) -> bool:
        if candidate is None or not candidate.output.text.strip():
            return False
        if candidate.assessment.percent < 75:
            return False
        if self._mean_confidence(candidate.output) < 0.85:
            return False
        observed = self._observed_scripts(candidate.output.text)
        allowed = _allowed_scripts(candidate.profile.languages)
        return not (observed - allowed)

    def _observe_language_strategy(
        self,
        candidates: tuple[_Candidate, ...],
        winner: _Candidate | None,
    ) -> None:
        by_profile: dict[str, list[_Candidate]] = {}
        for candidate in candidates:
            by_profile.setdefault(
                candidate.profile.profile_id,
                [],
            ).append(candidate)
        for profile_id, profile_candidates in by_profile.items():
            orders = tuple(dict.fromkeys(candidate.profile.languages for candidate in profile_candidates))
            if len(self._engine_variants.get(profile_id, ())) <= 1:
                continue
            best_by_order = {
                languages: max(
                    (candidate for candidate in profile_candidates if candidate.profile.languages == languages),
                    key=self._candidate_score,
                )
                for languages in orders
            }
            order_winner = max(
                best_by_order.values(),
                key=self._candidate_score,
            )
            self.state.observe_engine_order(
                profile_id,
                orders,
                order_winner.profile.languages,
                {
                    languages: self._candidate_score(candidate)[0] / 100.0
                    for languages, candidate in best_by_order.items()
                },
            )
        attempted_profiles = tuple(by_profile)
        if winner is not None:
            self.state.observe_fallback_profiles(
                attempted_profiles,
                winner.profile.profile_id,
                self._DEFAULT_PROFILE_ID,
            )

    @staticmethod
    def _terminal_exact(candidate: _Candidate) -> bool:
        alphanumeric = sum(character.isalnum() for character in candidate.output.text)
        return candidate.assessment.exact and alphanumeric >= 3

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
        payload = raw_png
        input_sha256 = hashlib.sha256(payload).hexdigest()
        try:
            if transform is OcrTransform.GAMMA:
                enhanced = self._enhancer.enhance_many((CropInput(f"{block_id}-gamma-attempt", raw_png),))[0]
                payload = enhanced.png_bytes
            else:
                payload = raw_png
            input_sha256 = hashlib.sha256(payload).hexdigest()
            self.state.remember_attempt_input(input_sha256, payload)

            def recognize_payload() -> OcrEngineOutput:
                output = self._worker(profile).recognize(payload)
                if not isinstance(output, OcrEngineOutput):
                    raise TypeError("language worker returned a non-OCR output")
                return output

            output, cache_hit = self._content_cache.recognize(
                OcrContentCacheKey(
                    actual_png_sha256=input_sha256,
                    adapter_id=profile.adapter_id,
                    config_sha256=profile.config_sha256,
                    profile_id=profile.profile_id,
                    transform=transform.value,
                    psm=profile.psm,
                    enhancer_id=(self._enhancer_id if transform is OcrTransform.GAMMA else "none"),
                ),
                recognize_payload,
            )
            assessment = assess_grammar(output, profile.languages)
            elapsed = time.perf_counter() - started
            self.state.record(
                block_id=block_id,
                unit_id=unit_id,
                profile=profile,
                transform=transform,
                input_sha256=input_sha256,
                status="cache-hit" if cache_hit else "complete",
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
                input_sha256=input_sha256,
                elapsed_seconds=elapsed,
                cache_hit=cache_hit,
            )
        except Exception as exc:
            elapsed = time.perf_counter() - started
            assessment = GrammarAssessment(0, False, ("ocr-error",))
            self.state.record(
                block_id=block_id,
                unit_id=unit_id,
                profile=profile,
                transform=transform,
                input_sha256=input_sha256,
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
        primary_profile_id: str | None = None,
        sweep_after_low_primary: bool = False,
        transforms: tuple[OcrTransform, ...] = (
            OcrTransform.RAW,
            OcrTransform.GAMMA,
        ),
        mixed_fallback_only: bool = False,
        fallback_profile_ids: tuple[str, ...] | None = None,
        run_primary: bool = True,
        ordered_mixed_profile: bool = False,
    ) -> _RecognitionDecision:
        profiles_by_id = {profile.profile_id: profile for profile in self._profiles}
        best: _Candidate | None = None
        profile_candidates: list[_Candidate] = []
        primary_profile_id = (
            primary_profile_id
            if primary_profile_id in profiles_by_id
            else (
                self.state.locked_profile_id
                if self.state.locked_profile_id in profiles_by_id
                else self._DEFAULT_PROFILE_ID
            )
        )
        if primary_profile_id not in profiles_by_id:
            primary_profile_id = self._profiles[0].profile_id
        allowed_fallback_ids = None if fallback_profile_ids is None else frozenset(fallback_profile_ids)
        profile_ids = ((primary_profile_id,) if run_primary else ()) + tuple(
            profile_id
            for profile_id in self.state.ordered_profile_ids()
            if profile_id != primary_profile_id
            and profile_id in profiles_by_id
            and (allowed_fallback_ids is None or profile_id in allowed_fallback_ids)
        )
        primary_grammar_percent = 0
        primary_raw_candidate: _Candidate | None = None
        primary_profile_candidate: _Candidate | None = None
        all_profiles_below_lock = not mixed_fallback_only
        profile_index = 0
        while profile_index < len(profile_ids):
            profile_id = profile_ids[profile_index]
            profile = profiles_by_id.get(profile_id)
            if profile is None:
                profile_index += 1
                continue
            is_primary_profile = profile_id == primary_profile_id
            profile_best: _Candidate | None = None
            profile_transforms = (
                transforms
                if is_primary_profile
                else tuple(transform for transform in transforms if transform is OcrTransform.RAW)
            )
            all_variants = (
                self._engine_variants.get(profile_id, (profile,))
                if ordered_mixed_profile and unit_id == "full-block"
                else (profile,)
            )
            active_orders = self.state.ordered_engine_orders(
                profile_id,
                tuple(variant.languages for variant in all_variants),
                maximum_losses=self._LANGUAGE_STRATEGY_MAXIMUM_LOSSES,
            )
            active_variants = tuple(
                variant for languages in active_orders for variant in all_variants if variant.languages == languages
            )
            attempted_variants: list[LanguageProfile] = []
            if len(all_variants) > 1 and OcrTransform.RAW in profile_transforms:
                for variant in active_variants:
                    candidate = self._attempt(
                        block_id=block_id,
                        unit_id=unit_id,
                        profile=variant,
                        transform=OcrTransform.RAW,
                        raw_png=raw_png,
                    )
                    attempted_variants.append(variant)
                    if candidate is not None:
                        profile_candidates.append(candidate)
                        if profile_best is None or self._candidate_score(candidate) > self._candidate_score(
                            profile_best
                        ):
                            profile_best = candidate
                        if candidate.assessment.percent >= (self._LOCK_GRAMMAR_PERCENT):
                            break
                if (
                    profile_best is not None
                    and self._observed_scripts(profile_best.output.text)
                    - _allowed_scripts(profile_best.profile.languages)
                    and len(active_variants) < len(all_variants)
                ):
                    self.state.reset_language_guards()
                    for variant in all_variants:
                        if variant in attempted_variants:
                            continue
                        candidate = self._attempt(
                            block_id=block_id,
                            unit_id=unit_id,
                            profile=variant,
                            transform=OcrTransform.RAW,
                            raw_png=raw_png,
                        )
                        if candidate is not None:
                            profile_candidates.append(candidate)
                            if self._candidate_score(candidate) > self._candidate_score(profile_best):
                                profile_best = candidate
                if is_primary_profile:
                    primary_raw_candidate = profile_best
                if (
                    OcrTransform.GAMMA in profile_transforms
                    and (
                        not ordered_mixed_profile
                        or unit_id != "full-block"
                        or not self._candidate_is_supported_homogeneous(profile_best)
                    )
                    and (profile_best is None or profile_best.assessment.percent < self._LOCK_GRAMMAR_PERCENT)
                ):
                    gamma_candidate = self._attempt(
                        block_id=block_id,
                        unit_id=unit_id,
                        profile=(profile_best.profile if profile_best is not None else active_variants[0]),
                        transform=OcrTransform.GAMMA,
                        raw_png=raw_png,
                    )
                    if gamma_candidate is not None:
                        profile_candidates.append(gamma_candidate)
                        if self._candidate_score(gamma_candidate) > self._candidate_score(profile_best):
                            profile_best = gamma_candidate
            else:
                for transform in profile_transforms:
                    candidate = self._attempt(
                        block_id=block_id,
                        unit_id=unit_id,
                        profile=profile,
                        transform=transform,
                        raw_png=raw_png,
                    )
                    if is_primary_profile and transform is OcrTransform.RAW and candidate is not None:
                        primary_raw_candidate = candidate
                    if candidate is not None and (
                        profile_best is None or self._candidate_score(candidate) > self._candidate_score(profile_best)
                    ):
                        profile_best = candidate
                    if candidate is not None and (
                        self._terminal_exact(candidate)
                        or (
                            transform is OcrTransform.RAW and candidate.assessment.percent >= self._LOCK_GRAMMAR_PERCENT
                        )
                    ):
                        break
            observed_percent = profile_best.assessment.percent if profile_best is not None else 0
            if profile_best is not None and profile_best.assessment.exact and not self._terminal_exact(profile_best):
                observed_percent = min(observed_percent, 99)
            self.state.observe(
                profile.profile_id,
                observed_percent,
            )
            if profile_best is not None and len(all_variants) <= 1:
                profile_candidates.append(profile_best)
            if is_primary_profile:
                primary_grammar_percent = observed_percent
                primary_profile_candidate = profile_best
            if profile_best is not None and (
                best is None or self._candidate_score(profile_best) > self._candidate_score(best)
            ):
                best = profile_best
            profile_reached_lock = (
                profile_best is not None and profile_best.assessment.percent >= self._LOCK_GRAMMAR_PERCENT
            )
            if profile_reached_lock:
                all_profiles_below_lock = False
            if is_primary_profile and profile_reached_lock:
                break
            if (
                is_primary_profile
                and len(all_variants) > 1
                and unit_id == "full-block"
                and ordered_mixed_profile
                and self._candidate_is_supported_homogeneous(profile_best)
                and not mixed_fallback_only
            ):
                general_fallback_ids = self.state.active_fallback_profile_ids(
                    ("rus", "eng"),
                    maximum_losses=(self._LANGUAGE_STRATEGY_MAXIMUM_LOSSES),
                )
                profile_ids = (primary_profile_id,) + tuple(
                    profile_id for profile_id in general_fallback_ids if profile_id in profiles_by_id
                )
            if is_primary_profile and mixed_fallback_only:
                mixed_fallback_ids = self._mixed_fallback_profile_ids(profile_best) if profile_best is not None else ()
                if allowed_fallback_ids is not None:
                    mixed_fallback_ids = tuple(
                        profile_id for profile_id in mixed_fallback_ids if profile_id in allowed_fallback_ids
                    )
                    if not mixed_fallback_ids:
                        mixed_fallback_ids = tuple(
                            profile_id
                            for profile_id in self.state.ordered_profile_ids()
                            if profile_id in allowed_fallback_ids and profile_id != primary_profile_id
                        )
                if not mixed_fallback_ids:
                    break
                profile_ids = (primary_profile_id,) + mixed_fallback_ids
            if not is_primary_profile and profile_reached_lock and not sweep_after_low_primary:
                break
            profile_index += 1
        # A confirmed, healthy document-language candidate is the stable
        # baseline.  Pure-script fallbacks can otherwise gain a few grammar
        # points by transliterating valid mixed-script tokens.  Keep collecting
        # their evidence, but only let them displace the locked primary when
        # they actually reach the target (or the primary itself is unhealthy).
        if (
            self.state.locked_profile_id == primary_profile_id
            and not self.state.lock_is_provisional
            and primary_profile_candidate is not None
            and best is not None
            and best.profile.profile_id != primary_profile_id
            and self._specialized_profile_kind(
                best.profile.profile_id,
                best.profile.languages,
            )
            is None
            and best.assessment.percent < self._LOCK_GRAMMAR_PERCENT
            and not self._candidate_requires_context_recursion(
                primary_profile_candidate,
                (),
            )
        ):
            best = primary_profile_candidate

        if unit_id == "full-block" and ordered_mixed_profile:
            self._observe_language_strategy(
                tuple(profile_candidates),
                best,
            )

        return _RecognitionDecision(
            candidate=best,
            profile_candidates=tuple(profile_candidates),
            primary_grammar_percent=primary_grammar_percent,
            all_profiles_below_lock=all_profiles_below_lock,
            primary_raw_candidate=primary_raw_candidate,
        )

    def _mixed_fallback_profile_ids(
        self,
        primary: _Candidate,
    ) -> tuple[str, ...]:
        observed = self._observed_scripts(primary.output.text)
        allowed = _allowed_scripts(primary.profile.languages)
        unexpected = observed - allowed
        desired: set[str] = set()
        if unexpected & {"cyrillic"}:
            desired.update(("rus-eng", "rus"))
        if unexpected & {"latin"}:
            desired.update(("rus-eng", "eng"))
        if unexpected & {"cjk", "hiragana", "katakana"}:
            desired.add("chi_sim")
        if unexpected & {"greek"}:
            desired.update(("ell", "equ"))
        if self._has_math_evidence(primary.output.text):
            desired.add("equ")
        return tuple(
            profile.profile_id
            for profile in self._profiles
            if profile.profile_id in desired and profile.profile_id != primary.profile.profile_id
        )

    @staticmethod
    def _has_math_evidence(text: str) -> bool:
        return any(symbol in text for symbol in ("∑", "√", "∫", "≠", "≤", "≥")) or bool(
            re.search(
                r"(?<!\w)(?:[A-Za-z]\w*|\d+(?:[.,]\d+)?)" r"\s*=\s*" r"(?:[A-Za-z]\w*|\d+(?:[.,]\d+)?)(?!\w)",
                text,
            )
        )

    @classmethod
    def _specialized_profile_kind(
        cls,
        profile_id: str,
        languages: tuple[str, ...],
    ) -> str | None:
        aliases = {
            profile_id.casefold(),
            *(language.casefold() for language in languages),
        }
        if aliases & cls._CJK_PROFILE_ALIASES:
            return "cjk"
        if aliases & cls._GREEK_PROFILE_ALIASES:
            return "greek"
        if aliases & cls._MATH_PROFILE_ALIASES:
            return "math"
        return None

    def _specialized_fallback_profile_ids(
        self,
        attempts: tuple[LanguageAttempt, ...],
    ) -> tuple[str, ...]:
        desired_kinds: set[str] = set()
        cjk_scripts = {"cjk", "hiragana", "katakana"}
        for attempt in attempts:
            if (
                attempt.unit_id != "full-block"
                or attempt.status not in {"complete", "cache-hit"}
                or not attempt.text.strip()
            ):
                continue
            kind = self._specialized_profile_kind(
                attempt.profile_id,
                attempt.languages,
            )
            if kind == "cjk":
                alphanumeric_count = sum(character.isalnum() for character in attempt.text)
                cjk_count = sum(
                    _character_script(character) in cjk_scripts for character in attempt.text if character.isalnum()
                )
                cjk_density = cjk_count / max(1, alphanumeric_count)
                if (
                    attempt.mean_confidence >= self._SPECIALIZED_MIN_MEAN_CONFIDENCE
                    and cjk_density >= self._SPECIALIZED_MIN_SCRIPT_DENSITY
                ):
                    desired_kinds.add("cjk")
                continue
            if kind is not None:
                continue
            observed = self._observed_scripts(attempt.text)
            if "greek" in observed:
                desired_kinds.add("greek")
            if self._has_math_evidence(attempt.text):
                desired_kinds.add("math")

        kind_by_profile_id = {
            profile.profile_id: self._specialized_profile_kind(
                profile.profile_id,
                profile.languages,
            )
            for profile in self._profiles
        }
        return tuple(
            profile_id
            for profile_id in self.state.ordered_profile_ids()
            if kind_by_profile_id.get(profile_id) in desired_kinds
        )

    @classmethod
    def _word_matches_native_script(
        cls,
        text: str,
        script_kind: str,
    ) -> bool:
        if script_kind == "cjk":
            return any(_character_script(character) in {"cjk", "hiragana", "katakana"} for character in text)
        if script_kind == "greek":
            return any(_character_script(character) == "greek" for character in text)
        if script_kind == "math":
            return cls._has_math_evidence(text) or any(character in "=+-−×÷*/^∑√∫≠≤≥" for character in text)
        return False

    def _candidate_has_local_native_evidence(
        self,
        candidate: _Candidate | None,
        script_kind: str,
        *,
        leaf: bool,
    ) -> bool:
        if candidate is None or not candidate.output.text.strip():
            return False
        alphanumeric = tuple(character for character in candidate.output.text if character.isalnum())
        native_count = sum(self._word_matches_native_script(character, script_kind) for character in alphanumeric)
        minimum_density = self._LOCAL_NATIVE_MIN_SCRIPT_DENSITY if leaf else self._SPECIALIZED_MIN_SCRIPT_DENSITY
        return (
            self._mean_confidence(candidate.output) >= self._SPECIALIZED_MIN_MEAN_CONFIDENCE
            and native_count >= self._LOCAL_NATIVE_MIN_CHARACTERS
            and native_count / max(1, len(alphanumeric)) >= minimum_density
        )

    @classmethod
    def _native_word_text(
        cls,
        text: str,
        script_kind: str,
    ) -> str:
        stripped = text.strip()
        if not stripped or not cls._word_matches_native_script(
            stripped,
            script_kind,
        ):
            return ""
        if script_kind == "math":
            return stripped
        native_scripts = {"cjk", "hiragana", "katakana"} if script_kind == "cjk" else {"greek"}
        if any(character.isalpha() and _character_script(character) not in native_scripts for character in stripped):
            return ""
        return stripped

    @classmethod
    def _native_runs_from_words(
        cls,
        words: tuple[OcrWord, ...],
        script_kind: str,
    ) -> tuple[_NativeScriptRun, ...]:
        runs = []
        current_texts: list[str] = []
        weighted_confidence = 0.0
        character_count = 0

        def flush() -> None:
            nonlocal weighted_confidence, character_count
            if not current_texts:
                return
            separator = "" if script_kind == "cjk" else " "
            runs.append(
                _NativeScriptRun(
                    script_kind=script_kind,
                    text=separator.join(current_texts),
                    confidence=weighted_confidence / max(1, character_count),
                )
            )
            current_texts.clear()
            weighted_confidence = 0.0
            character_count = 0

        for word in words:
            native_text = cls._native_word_text(word.text, script_kind)
            if not native_text:
                flush()
                continue
            weight = max(1, len(native_text.replace(" ", "")))
            current_texts.append(native_text)
            weighted_confidence += word.confidence * weight
            character_count += weight
        flush()
        return tuple(runs)

    def _native_script_runs(
        self,
        profile_candidates: tuple[_Candidate, ...],
        fallback_profile_ids: tuple[str, ...],
    ) -> tuple[_NativeScriptRun, ...]:
        candidate_by_profile_id = {candidate.profile.profile_id: candidate for candidate in profile_candidates}
        native_runs = []
        captured_kinds: set[str] = set()
        for profile_id in fallback_profile_ids:
            candidate = candidate_by_profile_id.get(profile_id)
            if candidate is None:
                continue
            script_kind = self._specialized_profile_kind(
                candidate.profile.profile_id,
                candidate.profile.languages,
            )
            if script_kind is None or script_kind in captured_kinds:
                continue
            captured_kinds.add(script_kind)
            native_runs.extend(
                self._native_runs_from_words(
                    candidate.output.words,
                    script_kind,
                )
            )
        return tuple(native_runs)

    def _native_script_unit_ids(
        self,
        profile_candidates: tuple[_Candidate, ...],
        fallback_profile_ids: tuple[str, ...],
        compaction: BlockCompaction | None,
    ) -> tuple[str, ...]:
        if compaction is None or not compaction.source_placements:
            return ()
        source_unit_ids = {artifact.unit_id for artifact in compaction.source_placements}
        candidate_by_profile_id = {candidate.profile.profile_id: candidate for candidate in profile_candidates}
        matched_unit_ids: set[str] = set()
        for profile_id in fallback_profile_ids:
            candidate = candidate_by_profile_id.get(profile_id)
            if candidate is None:
                continue
            script_kind = self._specialized_profile_kind(
                candidate.profile.profile_id,
                candidate.profile.languages,
            )
            if script_kind not in self._NATIVE_SCRIPT_EVIDENCE_KINDS:
                continue
            for word in candidate.output.words:
                if not self._native_word_text(word.text, script_kind):
                    continue
                overlaps = tuple(
                    (
                        self._intersection_area(
                            word.bbox,
                            placement.crop_bbox,
                        ),
                        -index,
                        placement.unit_id,
                    )
                    for index, placement in enumerate(compaction.placements)
                    if placement.unit_id in source_unit_ids
                )
                if not overlaps:
                    continue
                area, _inverse_index, unit_id = max(overlaps)
                if area > 0:
                    matched_unit_ids.add(unit_id)
        return tuple(placement.unit_id for placement in compaction.placements if placement.unit_id in matched_unit_ids)

    def _fallback_evidence_kinds(
        self,
        fallback_profile_ids: tuple[str, ...],
    ) -> frozenset[str]:
        profile_by_id = {profile.profile_id: profile for profile in self._profiles}
        return frozenset(
            script_kind
            for profile_id in fallback_profile_ids
            if (profile := profile_by_id.get(profile_id)) is not None
            if (
                script_kind := self._specialized_profile_kind(
                    profile.profile_id,
                    profile.languages,
                )
            )
            is not None
        )

    def _fallback_script_kinds(
        self,
        fallback_profile_ids: tuple[str, ...],
    ) -> frozenset[str]:
        return self._fallback_evidence_kinds(fallback_profile_ids) & self._NATIVE_SCRIPT_EVIDENCE_KINDS

    def _fallback_feature_kinds(
        self,
        fallback_profile_ids: tuple[str, ...],
    ) -> frozenset[str]:
        return self._fallback_evidence_kinds(fallback_profile_ids) & self._FEATURE_EVIDENCE_KINDS

    def _should_recurse_full_block(
        self,
        full_block: _FullBlockRecognition,
        *,
        allow_membership_calibration: bool,
    ) -> bool:
        if not self._uses_specialized_context_recursion(full_block):
            return allow_membership_calibration and full_block.all_profiles_below_lock
        return self._candidate_requires_context_recursion(
            full_block.candidate,
            full_block.fallback_profile_ids,
        )

    def _uses_specialized_context_recursion(
        self,
        full_block: _FullBlockRecognition,
    ) -> bool:
        return bool(
            self._fallback_script_kinds(full_block.fallback_profile_ids)
            or full_block.native_script_unit_ids
            or self._has_unsupported_unicode_script(full_block.candidate)
        )

    def _candidate_requires_context_recursion(
        self,
        candidate: _Candidate | None,
        fallback_profile_ids: tuple[str, ...],
        *,
        missing_native_script: bool | None = None,
    ) -> bool:
        if candidate is not None and candidate.assessment.percent >= self._LOCK_GRAMMAR_PERCENT:
            return False
        if candidate is None or not candidate.output.text.strip():
            return True
        if self._mean_confidence(candidate.output) < 0.85:
            return True

        observed = self._observed_scripts(candidate.output.text)
        evidenced_kinds = self._fallback_script_kinds(fallback_profile_ids)
        supported_scripts = set(_allowed_scripts(candidate.profile.languages))
        if "cjk" in evidenced_kinds:
            supported_scripts.update(("cjk", "hiragana", "katakana"))
        if "greek" in evidenced_kinds:
            supported_scripts.add("greek")
        if observed - supported_scripts:
            return True
        return (
            missing_native_script
            if missing_native_script is not None
            else self._missing_evidenced_native_script(
                candidate,
                fallback_profile_ids,
            )
        )

    def _contextual_composite_is_acceptable(
        self,
        candidate: _Candidate,
        fallback_profile_ids: tuple[str, ...],
    ) -> bool:
        return (
            candidate.transform is OcrTransform.CONTEXTUAL_COMPOSITE
            and not self._candidate_requires_context_recursion(
                candidate,
                fallback_profile_ids,
            )
        )

    def _missing_evidenced_native_script(
        self,
        candidate: _Candidate | None,
        fallback_profile_ids: tuple[str, ...],
    ) -> bool:
        evidenced_kinds = self._fallback_script_kinds(fallback_profile_ids)
        if not evidenced_kinds:
            return False
        if candidate is None or not candidate.output.text.strip():
            return True
        return any(
            not self._word_matches_native_script(
                candidate.output.text,
                script_kind,
            )
            for script_kind in evidenced_kinds
        )

    def _has_unsupported_unicode_script(
        self,
        candidate: _Candidate | None,
    ) -> bool:
        if candidate is None or not candidate.output.text.strip():
            return False
        return bool(self._observed_scripts(candidate.output.text) - _allowed_scripts(candidate.profile.languages))

    def _local_split_strategy_key(
        self,
        block: RecognitionBlock,
        full_block: _FullBlockRecognition,
    ) -> tuple[str | None, tuple[str, ...]] | None:
        if block.matrix_window_kind not in {
            "polar-local-full",
            "polar-local-signature",
        }:
            return None
        # Native-script and unexpected-script branches are correctness paths,
        # not homogeneous calibration samples.  They must retain deep
        # contextual recursion independently of what nearby local blocks did.
        if self._fallback_script_kinds(full_block.fallback_profile_ids):
            return None
        if self._has_unsupported_unicode_script(full_block.candidate):
            return None
        return (
            self.state.locked_profile_id,
            tuple(sorted(self._fallback_evidence_kinds(full_block.fallback_profile_ids))),
        )

    def _recursive_calibration_succeeded(
        self,
        recursive: _Candidate | None,
        full_block: _FullBlockRecognition,
    ) -> bool:
        if recursive is None or not self._contextual_composite_is_acceptable(
            recursive,
            full_block.fallback_profile_ids,
        ):
            return False
        full = full_block.candidate
        if full is None:
            return True
        return (
            recursive.assessment.percent,
            self._mean_confidence(recursive.output),
        ) > (
            full.assessment.percent,
            self._mean_confidence(full.output),
        )

    @staticmethod
    def _word_placement_overlap(word: OcrWord, placement: object) -> int:
        bbox = placement.crop_bbox
        return max(
            0,
            min(word.bbox.right, bbox.right) - max(word.bbox.left, bbox.left),
        ) * max(
            0,
            min(word.bbox.bottom, bbox.bottom) - max(word.bbox.top, bbox.top),
        )

    def _patch_missing_native_units(
        self,
        full: _Candidate | None,
        recursive: _Candidate | None,
        placements: tuple[CompactionPlacement, ...],
        fallback_profile_ids: tuple[str, ...],
    ) -> _Candidate | None:
        if full is None or recursive is None or not placements:
            return None
        evidenced_scripts = self._fallback_script_kinds(fallback_profile_ids)
        missing_scripts = tuple(
            script_kind
            for script_kind in evidenced_scripts
            if not self._word_matches_native_script(
                full.output.text,
                script_kind,
            )
        )
        if not missing_scripts:
            return None

        replacements: dict[str, list[OcrWord]] = {}
        for word in recursive.output.words:
            native_kind = next(
                (script_kind for script_kind in missing_scripts if self._native_word_text(word.text, script_kind)),
                None,
            )
            if native_kind is None:
                continue
            placement = max(
                placements,
                key=lambda item: (
                    self._word_placement_overlap(word, item),
                    -item.crop_bbox.top,
                    -item.crop_bbox.left,
                ),
            )
            if self._word_placement_overlap(word, placement) <= 0:
                continue
            replacements.setdefault(placement.unit_id, []).append(word)

        replacements = {
            unit_id: words
            for unit_id, words in replacements.items()
            if sum(len("".join(character for character in word.text if character.isalnum())) for word in words)
            >= self._LOCAL_NATIVE_MIN_CHARACTERS
        }
        if not replacements:
            return None

        placement_by_id = {
            placement.unit_id: placement for placement in placements if placement.unit_id in replacements
        }

        def target_unit_id(word: OcrWord) -> str | None:
            if not placement_by_id:
                return None
            unit_id, overlap = max(
                (
                    (
                        unit_id,
                        self._word_placement_overlap(word, placement),
                    )
                    for unit_id, placement in placement_by_id.items()
                ),
                key=lambda item: item[1],
            )
            return unit_id if overlap > 0 else None

        full_targets = tuple(target_unit_id(word) for word in full.output.words)
        matched_units = {unit_id for unit_id in full_targets if unit_id}
        replacements = {unit_id: words for unit_id, words in replacements.items() if unit_id in matched_units}
        if not replacements:
            return None

        words = []
        emitted_units: set[str] = set()
        for word, unit_id in zip(full.output.words, full_targets):
            if unit_id not in replacements:
                words.append(word)
                continue
            if unit_id in emitted_units:
                continue
            emitted_units.add(unit_id)
            deduplicated = {(native.text, native.bbox.as_tuple()): native for native in replacements[unit_id]}
            words.extend(deduplicated.values())
        if not emitted_units:
            return None
        patched_output = OcrEngineOutput(
            text=" ".join(word.text for word in words),
            words=tuple(words),
            geometry=full.output.geometry,
        )
        return replace(full, output=patched_output)

    @classmethod
    def _recursive_native_runs(
        cls,
        words: tuple[OcrWord, ...],
    ) -> tuple[_RecursiveNativeRun, ...]:
        runs = []
        start_index = 0
        current_kind: str | None = None
        current_texts: list[str] = []
        current_words: list[OcrWord] = []

        def flush(stop_index: int) -> None:
            nonlocal current_kind
            if current_kind is None or not current_words:
                return
            separator = "" if current_kind == "cjk" else " "
            weights = tuple(max(1, len(text.replace(" ", ""))) for text in current_texts)
            runs.append(
                _RecursiveNativeRun(
                    script_kind=current_kind,
                    start_index=start_index,
                    stop_index=stop_index,
                    text=separator.join(current_texts),
                    confidence=sum(word.confidence * weight for word, weight in zip(current_words, weights))
                    / max(1, sum(weights)),
                    bbox=Box.union(word.bbox for word in current_words),
                )
            )
            current_kind = None
            current_texts.clear()
            current_words.clear()

        for index, word in enumerate(words):
            kind_and_text = next(
                (
                    (script_kind, native_text)
                    for script_kind in ("cjk", "greek", "math")
                    if (
                        native_text := cls._native_word_text(
                            word.text,
                            script_kind,
                        )
                    )
                ),
                None,
            )
            if kind_and_text is None:
                flush(index)
                continue
            script_kind, native_text = kind_and_text
            if current_kind != script_kind:
                flush(index)
                start_index = index
                current_kind = script_kind
            current_texts.append(native_text)
            current_words.append(word)
        flush(len(words))
        return tuple(runs)

    @staticmethod
    def _native_run_similarity(
        recursive: _RecursiveNativeRun,
        specialized: _NativeScriptRun,
    ) -> float | None:
        if recursive.script_kind != specialized.script_kind or specialized.confidence + 1e-9 < recursive.confidence:
            return None
        recursive_text = "".join(recursive.text.casefold().split())
        specialized_text = "".join(specialized.text.casefold().split())
        if not recursive_text or not specialized_text:
            return None
        matcher = difflib.SequenceMatcher(
            None,
            recursive_text,
            specialized_text,
            autojunk=False,
        )
        similarity = matcher.ratio()
        common = matcher.find_longest_match(
            0,
            len(recursive_text),
            0,
            len(specialized_text),
        ).size
        return similarity if similarity >= 0.5 and common >= 2 else None

    def _monotonic_native_run_alignment(
        self,
        recursive_runs: tuple[_RecursiveNativeRun, ...],
        specialized_runs: tuple[_NativeScriptRun, ...],
    ) -> tuple[tuple[int, int], ...]:
        rows = len(recursive_runs)
        columns = len(specialized_runs)
        scores = [[0.0] * (columns + 1) for _ in range(rows + 1)]
        choices = [["done"] * columns for _ in range(rows)]
        for row in range(rows - 1, -1, -1):
            for column in range(columns - 1, -1, -1):
                options = [
                    (scores[row + 1][column], 0, "skip-recursive"),
                    (scores[row][column + 1], 1, "skip-specialized"),
                ]
                similarity = self._native_run_similarity(
                    recursive_runs[row],
                    specialized_runs[column],
                )
                if similarity is not None:
                    options.append(
                        (
                            1.0 + similarity + scores[row + 1][column + 1],
                            2,
                            "match",
                        )
                    )
                score, _, choice = max(options, key=lambda item: (item[0], item[1]))
                scores[row][column] = score
                choices[row][column] = choice
        matches = []
        row = 0
        column = 0
        while row < rows and column < columns:
            choice = choices[row][column]
            if choice == "match":
                matches.append((row, column))
                row += 1
                column += 1
            elif choice == "skip-specialized":
                column += 1
            else:
                row += 1
        return tuple(matches)

    def _reconcile_native_runs(
        self,
        recursive_output: OcrEngineOutput,
        specialized_runs: tuple[_NativeScriptRun, ...],
    ) -> OcrEngineOutput:
        recursive_runs = self._recursive_native_runs(recursive_output.words)
        replacements: dict[int, tuple[int, OcrWord]] = {}
        for script_kind in ("cjk", "greek", "math"):
            recursive_subset = tuple(run for run in recursive_runs if run.script_kind == script_kind)
            specialized_subset = tuple(run for run in specialized_runs if run.script_kind == script_kind)
            for recursive_index, specialized_index in self._monotonic_native_run_alignment(
                recursive_subset,
                specialized_subset,
            ):
                recursive_run = recursive_subset[recursive_index]
                specialized_run = specialized_subset[specialized_index]
                replacements[recursive_run.start_index] = (
                    recursive_run.stop_index,
                    OcrWord(
                        specialized_run.text,
                        recursive_run.bbox,
                        specialized_run.confidence,
                    ),
                )
        if not replacements:
            return recursive_output
        words = []
        index = 0
        while index < len(recursive_output.words):
            replacement = replacements.get(index)
            if replacement is None:
                words.append(recursive_output.words[index])
                index += 1
                continue
            stop_index, word = replacement
            words.append(word)
            index = stop_index
        return OcrEngineOutput(
            text=" ".join(word.text for word in words),
            words=tuple(words),
            geometry=recursive_output.geometry,
        )

    def _recognize_isolated(
        self,
        *,
        block_id: str,
        unit_id: str,
        raw_png: bytes,
        nodes: tuple[SplayLanguageNode, ...],
        locked_profile_id: str | None,
        mixed_fallback_only: bool,
        fallback_profile_ids: tuple[str, ...] | None = None,
        run_primary: bool = True,
        ordered_mixed_profile: bool = False,
    ) -> _IsolatedRecognition:
        started = time.perf_counter()
        child = object.__new__(AdaptivePersistentOcrSession)
        child._profiles = self._profiles
        child._engine_variants = self._engine_variants
        child._max_workers = 1
        child.state = LanguageSplayState(self._profiles)
        child.state.share_attempt_input_artifacts(self.state)
        child.state.nodes = [replace(node) for node in nodes]
        child.state.copy_language_strategy_from(self.state)
        child.state.locked_profile_id = locked_profile_id
        child.log_path = None
        child._workers = {}
        child._worker_errors = {}
        child._content_cache = self._content_cache
        child._unit_candidate_cache = {}
        child._group_candidate_cache = {}
        child._source_line_candidate_cache = self._source_line_candidate_cache
        child._enhancer = GammaDarkCropEnhancer()
        child._enhancer_id = self._enhancer_id
        child._closed = False
        try:
            decision = child._recognize_image(
                block_id=block_id,
                unit_id=unit_id,
                raw_png=raw_png,
                primary_profile_id=locked_profile_id,
                transforms=(OcrTransform.RAW, OcrTransform.GAMMA),
                mixed_fallback_only=mixed_fallback_only,
                fallback_profile_ids=fallback_profile_ids,
                run_primary=run_primary,
                ordered_mixed_profile=ordered_mixed_profile,
            )
            return _IsolatedRecognition(
                candidate=decision.candidate,
                profile_candidates=decision.profile_candidates,
                attempts=tuple(child.state.attempts),
                observations=tuple(child.state.observations),
                elapsed_seconds=time.perf_counter() - started,
                all_profiles_below_lock=(decision.all_profiles_below_lock),
                primary_raw_candidate=decision.primary_raw_candidate,
            )
        finally:
            child.close()

    def _recognize_recursive_isolated(
        self,
        *,
        block_id: str,
        block_bbox: Box,
        crop: BlockCropPair,
        compaction: BlockCompaction,
        fallback_profile_ids: tuple[str, ...],
        native_script_unit_ids: frozenset[str],
        nodes: tuple[SplayLanguageNode, ...],
        locked_profile_id: str | None,
        lock_is_provisional: bool,
        contextual: bool,
    ) -> _IsolatedRecursiveRecognition:
        child = object.__new__(AdaptivePersistentOcrSession)
        child._profiles = self._profiles
        child._engine_variants = self._engine_variants
        child._max_workers = 1
        child.state = LanguageSplayState(self._profiles)
        child.state.share_attempt_input_artifacts(self.state)
        child.state.nodes = [replace(node) for node in nodes]
        child.state.copy_language_strategy_from(self.state)
        child.state.locked_profile_id = locked_profile_id
        child.state.lock_is_provisional = lock_is_provisional
        child.log_path = None
        child._workers = {}
        child._worker_errors = {}
        child._content_cache = self._content_cache
        child._unit_candidate_cache = {}
        child._group_candidate_cache = {}
        child._source_line_candidate_cache = self._source_line_candidate_cache
        child._enhancer = GammaDarkCropEnhancer()
        child._enhancer_id = self._enhancer_id
        child._closed = False
        try:
            if contextual:
                candidate = child._recursive_candidate(
                    block_id=block_id,
                    block_bbox=block_bbox,
                    crop=crop,
                    compaction=compaction,
                    fallback_profile_ids=fallback_profile_ids,
                    native_script_unit_ids=native_script_unit_ids,
                )
            else:
                candidate = child._membership_recursive_candidate(
                    block_id=block_id,
                    block_bbox=block_bbox,
                    crop=crop,
                    compaction=compaction,
                )
            return _IsolatedRecursiveRecognition(
                candidate=candidate,
                attempts=tuple(child.state.attempts),
                observations=tuple(child.state.observations),
            )
        finally:
            child.close()

    def _recognize_blocks(
        self,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        compaction_by_id: dict[str, BlockCompaction] | None = None,
    ) -> tuple[_FullBlockRecognition, ...]:
        if not plan.blocks:
            return ()
        local_kinds = {"polar-local-full", "polar-local-signature"}
        initialize_document_lock = self.state.locked_profile_id is None
        first_started = time.perf_counter()
        first_attempt_start = len(self.state.attempts)
        first = self._recognize_image(
            block_id=plan.blocks[0].block_id,
            unit_id="full-block",
            raw_png=crops[0].raw.png_bytes,
            primary_profile_id=(self._DEFAULT_PROFILE_ID if initialize_document_lock else self.state.locked_profile_id),
            sweep_after_low_primary=initialize_document_lock,
            transforms=(OcrTransform.RAW, OcrTransform.GAMMA),
            mixed_fallback_only=not initialize_document_lock,
            fallback_profile_ids=(
                (self._DEFAULT_PROFILE_ID,)
                if not initialize_document_lock and plan.blocks[0].matrix_window_kind != "dyadic-mask"
                else None
            ),
            ordered_mixed_profile=(plan.blocks[0].matrix_window_kind in local_kinds),
        )
        first_attempts = tuple(self.state.attempts[first_attempt_start:])
        if initialize_document_lock:
            self.state.locked_profile_id = (
                first.candidate.profile.profile_id if first.candidate is not None else self._DEFAULT_PROFILE_ID
            )
            self.state.lock_is_provisional = first.primary_grammar_percent < self._LOCK_GRAMMAR_PERCENT

        def materialize(
            index: int,
            decision: _RecognitionDecision | _IsolatedRecognition,
            attempts: tuple[LanguageAttempt, ...],
            elapsed_seconds: float,
        ) -> _FullBlockRecognition:
            fallback_profile_ids = self._specialized_fallback_profile_ids(attempts)
            candidate = decision.candidate
            primary_raw = decision.primary_raw_candidate
            compaction = (compaction_by_id or {}).get(plan.blocks[index].block_id)
            if (
                plan.blocks[index].matrix_window_kind in local_kinds
                and primary_raw is not None
                and (
                    candidate is None
                    or self._candidate_requires_context_recursion(
                        candidate,
                        fallback_profile_ids,
                    )
                    # Local membership fusion needs a RAW control.  Gamma is
                    # selected only when it improves grammar, not for a tiny
                    # confidence-only tie over the same recognized text.
                    or candidate.assessment.percent <= primary_raw.assessment.percent
                )
            ):
                candidate = primary_raw
            return _FullBlockRecognition(
                candidate=candidate,
                elapsed_seconds=elapsed_seconds,
                all_profiles_below_lock=decision.all_profiles_below_lock,
                fallback_profile_ids=fallback_profile_ids,
                native_script_runs=self._native_script_runs(
                    decision.profile_candidates,
                    fallback_profile_ids,
                ),
                native_script_unit_ids=self._native_script_unit_ids(
                    decision.profile_candidates,
                    fallback_profile_ids,
                    compaction,
                ),
                primary_raw_candidate=primary_raw,
            )

        first_full = materialize(
            0,
            first,
            first_attempts,
            time.perf_counter() - first_started,
        )
        results: dict[int, _FullBlockRecognition] = {0: first_full}
        profile_by_id = {profile.profile_id: profile for profile in self._profiles}
        specialized_probe_profile_ids = tuple(
            profile_id
            for profile_id in self.state.ordered_profile_ids()
            if profile_id in profile_by_id
            and self._specialized_profile_kind(
                profile_id,
                profile_by_id[profile_id].languages,
            )
            is not None
        )[:1]

        def run_phase(
            indexes: tuple[int, ...],
            *,
            fast_local: bool,
        ) -> dict[int, _IsolatedRecognition]:
            if not indexes:
                return {}
            nodes = tuple(replace(node) for node in self.state.nodes)
            with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers) as executor:
                futures = tuple(
                    (
                        index,
                        executor.submit(
                            self._recognize_isolated,
                            block_id=plan.blocks[index].block_id,
                            unit_id="full-block",
                            raw_png=crops[index].raw.png_bytes,
                            nodes=nodes,
                            locked_profile_id=self.state.locked_profile_id,
                            mixed_fallback_only=True,
                            fallback_profile_ids=(
                                tuple(
                                    dict.fromkeys(
                                        (
                                            self._DEFAULT_PROFILE_ID,
                                            *specialized_probe_profile_ids,
                                        )
                                    )
                                )
                                if plan.blocks[index].matrix_window_kind == "dyadic-mask"
                                else (self._DEFAULT_PROFILE_ID,)
                            ),
                            ordered_mixed_profile=(plan.blocks[index].matrix_window_kind in local_kinds),
                        ),
                    )
                    for index in indexes
                )
                isolated = tuple((index, future.result()) for index, future in futures)
            by_index = {}
            for index, item in isolated:
                self.state.merge_attempts(item.attempts)
                for profile_id, grammar_percent in item.observations:
                    self.state.observe(profile_id, grammar_percent)
                if plan.blocks[index].matrix_window_kind in local_kinds:
                    self._observe_language_strategy(
                        item.profile_candidates,
                        item.candidate,
                    )
                results[index] = materialize(
                    index,
                    item,
                    item.attempts,
                    item.elapsed_seconds,
                )
                by_index[index] = item
            return by_index

        local_indexes = tuple(
            index for index, block in enumerate(plan.blocks) if block.matrix_window_kind in local_kinds
        )
        calibration_indexes = local_indexes[: self._SPLIT_CALIBRATION_SAMPLES]
        calibration_frontier = max(calibration_indexes) if calibration_indexes else 0
        first_phase_indexes = tuple(range(1, min(len(plan.blocks), calibration_frontier + 1)))
        first_phase = run_phase(first_phase_indexes, fast_local=False)
        self.state.finalize_engine_order_calibration(
            self._DEFAULT_PROFILE_ID,
            minimum_samples=self._LANGUAGE_STRATEGY_MAXIMUM_LOSSES,
            maximum_losses=self._LANGUAGE_STRATEGY_MAXIMUM_LOSSES,
        )

        calibration_decisions: dict[
            int,
            _RecognitionDecision | _IsolatedRecognition,
        ] = {0: first}
        calibration_decisions.update(first_phase)
        calibration_successes = 0
        native_script_evidence = False
        for index in calibration_indexes:
            full = results[index]
            native_script_evidence = native_script_evidence or bool(
                self._fallback_script_kinds(full.fallback_profile_ids)
            )
            decision = calibration_decisions[index]
            candidate = decision.candidate
            primary_raw = decision.primary_raw_candidate
            if (
                candidate is not None
                and primary_raw is not None
                and candidate is not primary_raw
                and not self._candidate_requires_context_recursion(
                    candidate,
                    full.fallback_profile_ids,
                )
                and (
                    candidate.assessment.percent,
                    self._mean_confidence(candidate.output),
                )
                > (
                    primary_raw.assessment.percent,
                    self._mean_confidence(primary_raw.output),
                )
            ):
                calibration_successes += 1
        fast_local = (
            len(calibration_indexes) >= self._SPLIT_CALIBRATION_SAMPLES
            and not native_script_evidence
            and calibration_successes < self._SPLIT_CALIBRATION_MIN_SUCCESSES
        )
        remaining_indexes = tuple(range(calibration_frontier + 1, len(plan.blocks)))
        run_phase(remaining_indexes, fast_local=fast_local)
        return tuple(results[index] for index in range(len(plan.blocks)))

    @staticmethod
    def _intersection_area(left: Box, right: Box) -> int:
        width = max(0, min(left.right, right.right) - max(left.left, right.left))
        height = max(0, min(left.bottom, right.bottom) - max(left.top, right.top))
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
        left = source.left - block_bbox.left + round((word.bbox.left - crop.left) * scale_x)
        top = source.top - block_bbox.top + round((word.bbox.top - crop.top) * scale_y)
        right = source.left - block_bbox.left + round((word.bbox.right - crop.left) * scale_x)
        bottom = source.top - block_bbox.top + round((word.bbox.bottom - crop.top) * scale_y)
        left = max(0, min(block_bbox.width - 1, left))
        top = max(0, min(block_bbox.height - 1, top))
        right = max(left + 1, min(block_bbox.width, right))
        bottom = max(top + 1, min(block_bbox.height, bottom))
        return OcrWord(
            text=word.text,
            bbox=Box(left, top, right, bottom),
            confidence=word.confidence,
        )

    @staticmethod
    def _canonical_membership_contract(
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        compactions: tuple[BlockCompaction, ...],
    ) -> dict[str, tuple[dict[str, int], tuple[int, int]]]:
        compaction_by_id = {compaction.block_id: compaction for compaction in compactions}
        crop_by_id = {crop.block_id: crop for crop in crops}
        canonical_block_ids = tuple(
            block.block_id
            for block in plan.blocks
            if (
                (compaction := compaction_by_id.get(block.block_id)) is not None
                and getattr(compaction, "raster_kind", None) is CompactionRasterKind.CANONICAL_LOCALITY
            )
        )
        if not canonical_block_ids:
            return {}
        missing_crop_ids = tuple(block_id for block_id in canonical_block_ids if block_id not in crop_by_id)
        if missing_crop_ids:
            raise ValueError("canonical locality block has no bound crop")
        component_by_block_id = _membership_component_by_block_id(plan)
        block_ids_by_component: dict[str, list[str]] = {}
        for block_id in canonical_block_ids:
            block_ids_by_component.setdefault(
                component_by_block_id[block_id],
                [],
            ).append(block_id)
        contracts: dict[
            str,
            tuple[dict[str, int], tuple[int, int]],
        ] = {}
        for component_id, component_block_ids in block_ids_by_component.items():
            component_block_id_set = set(component_block_ids)
            unit_ids = tuple(
                dict.fromkeys(
                    tuple(
                        unit.unit_id
                        for unit in plan.membership_units
                        if component_block_id_set.intersection(unit.block_ids)
                    )
                    + tuple(
                        placement.unit_id
                        for block_id in component_block_ids
                        for placement in compaction_by_id[block_id].placements
                    )
                )
            )
            slot_by_id = {unit_id: slot for slot, unit_id in enumerate(unit_ids)}
            component_crops = tuple(crop_by_id[block_id] for block_id in component_block_ids)
            contract = (
                slot_by_id,
                (
                    min(crop.bbox.width for crop in component_crops),
                    min(crop.bbox.height for crop in component_crops),
                ),
            )
            for block_id in component_block_ids:
                contracts[block_id] = contract
        return contracts

    def _map_full_output(
        self,
        output: OcrEngineOutput,
        compaction: BlockCompaction | None,
        block_bbox: Box,
        membership_slot_by_id: dict[str, int],
        canonical_slot_size: tuple[int, int],
    ) -> OcrEngineOutput:
        if compaction is None or not compaction.placements or not output.words:
            return output
        if getattr(compaction, "raster_kind", None) is CompactionRasterKind.CANONICAL_LOCALITY:
            slot_width, slot_height = canonical_slot_size
            if slot_width <= 0 or slot_height <= 0 or len(membership_slot_by_id) > slot_width * slot_height:
                raise ValueError("canonical locality membership slots exceed bound crop")
            grouped: dict[str, list[tuple[int, OcrWord]]] = {}
            for order, word in enumerate(output.words):
                placement = max(
                    compaction.placements,
                    key=lambda item: (
                        self._intersection_area(word.bbox, item.crop_bbox),
                        -abs((word.bbox.left + word.bbox.right) - (item.crop_bbox.left + item.crop_bbox.right)),
                    ),
                )
                if (
                    self._intersection_area(
                        word.bbox,
                        placement.crop_bbox,
                    )
                    <= 0
                ):
                    continue
                grouped.setdefault(placement.unit_id, []).append((order, word))
            mapped_with_order = []
            for placement in compaction.placements:
                words = grouped.get(placement.unit_id)
                if not words:
                    continue
                slot = membership_slot_by_id.get(placement.unit_id)
                if slot is None:
                    raise ValueError("canonical locality placement is outside membership plan")
                left = slot % slot_width
                top = slot // slot_width
                text = " ".join(word.text for _, word in words)
                weights = [max(1, len(word.text)) for _, word in words]
                confidence = sum(word.confidence * weight for (_, word), weight in zip(words, weights)) / sum(weights)
                mapped_with_order.append(
                    (
                        words[0][0],
                        OcrWord(
                            text=text,
                            bbox=Box(left, top, left + 1, top + 1),
                            confidence=confidence,
                        ),
                    )
                )
            mapped = tuple(word for _, word in sorted(mapped_with_order))
            return OcrEngineOutput(
                text=" ".join(word.text for word in mapped),
                words=mapped,
                geometry=OcrOutputGeometry.WORD_BOXES,
            )
        mapped = []
        for word in output.words:
            placement = max(
                compaction.placements,
                key=lambda item: (
                    self._intersection_area(word.bbox, item.crop_bbox),
                    -abs((word.bbox.left + word.bbox.right) - (item.crop_bbox.left + item.crop_bbox.right)),
                ),
            )
            mapped.append(self._map_word(word, placement, block_bbox))
        return OcrEngineOutput(
            text=" ".join(word.text for word in mapped),
            words=tuple(mapped),
            geometry=OcrOutputGeometry.WORD_BOXES,
        )

    def _build_context_group(
        self,
        *,
        block_id: str,
        depth: int,
        first_order: int,
        tiles: tuple[_Tile, ...],
    ) -> _ContextGroup:
        canonical_flags = tuple(tile.canonical_bbox is not None for tile in tiles)
        if any(canonical_flags):
            if not all(canonical_flags):
                raise ValueError("context group cannot mix canonical and spatial tiles")
            rendered = _crop_canonical_locality_subset(tiles)
            payload = rendered.png_bytes
            placements = rendered.placements
        else:
            payload, placements, _, _ = _pack_tiles(tiles)
        return _ContextGroup(
            group_id=(f"{block_id}:context-depth-{depth:02d}:" f"order-{first_order:08d}:units-{len(tiles):03d}"),
            depth=depth,
            first_order=first_order,
            tiles=tiles,
            png_bytes=payload,
            placements=placements,
        )

    @staticmethod
    def _decode_source_placement(
        artifact: SourcePlacementArtifact,
    ) -> np.ndarray | None:
        if hashlib.sha256(artifact.png_bytes).hexdigest() != artifact.sha256:
            return None
        try:
            with Image.open(io.BytesIO(artifact.png_bytes)) as opened:
                opened.load()
                if opened.format != "PNG" or opened.size != (
                    artifact.source_bbox.width,
                    artifact.source_bbox.height,
                ):
                    return None
                return np.asarray(opened.convert("RGB")).copy()
        except Exception:
            return None

    def _build_source_placement_line(
        self,
        *,
        block_id: str,
        leaf_group: _ContextGroup,
        source_placements: tuple[SourcePlacementArtifact, ...],
    ) -> _ContextGroup | None:
        if len(leaf_group.tiles) != 1 or not source_placements:
            return None
        anchor_id = leaf_group.tiles[0].unit_id
        by_id = {item.unit_id: item for item in source_placements}
        anchor = by_id.get(anchor_id)
        if anchor is None:
            return None
        same_column = tuple(
            item
            for item in source_placements
            if item.island_id == anchor.island_id and item.matrix_column == anchor.matrix_column
        )
        same_row = tuple(
            item
            for item in source_placements
            if item.island_id == anchor.island_id and item.matrix_row == anchor.matrix_row
        )
        candidates = same_column if len(same_column) >= 2 else same_row
        ranked = sorted(
            candidates,
            key=lambda item: (
                abs(item.matrix_row - anchor.matrix_row),
                abs(item.matrix_column - anchor.matrix_column),
                abs(item.polar_order - anchor.polar_order),
                item.polar_order,
                item.unit_id,
            ),
        )[:4]
        if anchor not in ranked:
            ranked = sorted(
                (*ranked[:3], anchor),
                key=lambda item: (
                    abs(item.matrix_row - anchor.matrix_row),
                    abs(item.matrix_column - anchor.matrix_column),
                    abs(item.polar_order - anchor.polar_order),
                    item.polar_order,
                    item.unit_id,
                ),
            )
        selected = tuple(
            sorted(
                ranked,
                key=lambda item: (
                    item.matrix_row,
                    item.matrix_column,
                    item.polar_order,
                    item.unit_id,
                ),
            )
        )
        if len(selected) < 2:
            return None
        decoded = []
        backgrounds = []
        for artifact in selected:
            pixels = self._decode_source_placement(artifact)
            if pixels is None or not pixels.size:
                return None
            border = np.concatenate(
                (
                    pixels[0, :, :],
                    pixels[-1, :, :],
                    pixels[:, 0, :],
                    pixels[:, -1, :],
                ),
                axis=0,
            )
            backgrounds.append(np.median(border, axis=0))
            decoded.append((artifact, pixels))
        median_height = float(np.median([pixels.shape[0] for _, pixels in decoded]))
        gutter = max(4, min(32, int(round(median_height * 0.25))))
        background = np.rint(np.median(np.asarray(backgrounds), axis=0)).astype(np.uint8)
        width = sum(pixels.shape[1] for _, pixels in decoded)
        width += gutter * (len(decoded) + 1)
        height = max(pixels.shape[0] for _, pixels in decoded) + 2 * gutter
        if width * height > 2_000_000:
            return None
        canvas = np.empty((height, width, 3), dtype=np.uint8)
        canvas[:, :] = background
        placements = []
        tiles = []
        left = gutter
        for artifact, pixels in decoded:
            tile_height, tile_width = pixels.shape[:2]
            top = gutter + (height - 2 * gutter - tile_height) // 2
            canvas[top : top + tile_height, left : left + tile_width] = pixels
            placements.append(
                CompactionPlacement(
                    unit_id=artifact.unit_id,
                    segment_ids=artifact.segment_ids,
                    source_bbox=artifact.source_bbox,
                    crop_bbox=Box(
                        left,
                        top,
                        left + tile_width,
                        top + tile_height,
                    ),
                )
            )
            tiles.append(
                _Tile(
                    unit_id=artifact.unit_id,
                    segment_ids=artifact.segment_ids,
                    source_bbox=artifact.source_bbox,
                    pixels=pixels,
                )
            )
            left += tile_width + gutter
        payload = _png_bytes(canvas)
        return _ContextGroup(
            group_id=(f"{block_id}:source-placement-line:anchor-{anchor_id}:" f"units-{len(tiles):03d}"),
            depth=leaf_group.depth,
            first_order=leaf_group.first_order,
            tiles=tuple(tiles),
            png_bytes=payload,
            placements=tuple(placements),
            emit_unit_ids=(anchor_id,),
            source_fallback=True,
        )

    def _source_line_covers_anchor(
        self,
        group: _ContextGroup,
        candidate: _Candidate | None,
        script_kinds: frozenset[str],
    ) -> bool:
        if candidate is None or not group.emit_unit_ids:
            return False
        targets = tuple(placement for placement in group.placements if placement.unit_id in group.emit_unit_ids)
        if not targets:
            return False
        return any(
            word.confidence >= self._SPECIALIZED_MIN_MEAN_CONFIDENCE
            and any(self._native_word_text(word.text, script_kind) for script_kind in script_kinds)
            and any(self._intersection_area(word.bbox, placement.crop_bbox) > 0 for placement in targets)
            for word in candidate.output.words
        )

    def _source_line_candidate(
        self,
        *,
        block_id: str,
        group: _ContextGroup,
        fallback_policy: tuple[str, ...],
        script_kinds: frozenset[str],
        require_native_coverage: bool = True,
    ) -> _Candidate | None:
        source_digest = hashlib.sha256(group.png_bytes).hexdigest()
        best: _Candidate | None = None
        for profile_id in fallback_policy:
            for engine_transform in (OcrTransform.RAW, OcrTransform.GAMMA):
                key = (source_digest, profile_id, engine_transform)

                def recognize(
                    profile_id: str = profile_id,
                    engine_transform: OcrTransform = engine_transform,
                ) -> _Candidate | None:
                    attempt_start = len(self.state.attempts)
                    decision = self._recognize_image(
                        block_id=block_id,
                        unit_id=group.group_id,
                        raw_png=group.png_bytes,
                        primary_profile_id=self.state.locked_profile_id,
                        transforms=(engine_transform,),
                        mixed_fallback_only=False,
                        fallback_profile_ids=(profile_id,),
                        run_primary=False,
                    )
                    for index in range(
                        attempt_start,
                        len(self.state.attempts),
                    ):
                        self.state.attempts[index] = replace(
                            self.state.attempts[index],
                            transform=(OcrTransform.SOURCE_PLACEMENT_FALLBACK),
                        )
                    candidate = decision.candidate
                    if candidate is None:
                        return None
                    return replace(
                        candidate,
                        transform=OcrTransform.SOURCE_PLACEMENT_FALLBACK,
                    )

                candidate = self._source_line_candidate_cache.resolve(
                    key,
                    recognize,
                )
                if candidate is None:
                    continue
                if best is None or self._candidate_score(candidate) > self._candidate_score(best):
                    best = candidate
                if require_native_coverage:
                    if self._source_line_covers_anchor(
                        group,
                        candidate,
                        script_kinds,
                    ):
                        return candidate
                    continue
                if candidate.assessment.percent >= self._LOCK_GRAMMAR_PERCENT or (
                    candidate.output.text.strip() and self._mean_confidence(candidate.output) >= 0.85
                ):
                    return candidate
        return None if require_native_coverage else best

    def _ordered_context_fallback_policy(
        self,
        fallback_profile_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        allowed = frozenset(fallback_profile_ids)
        return tuple(profile_id for profile_id in self.state.ordered_profile_ids() if profile_id in allowed)

    def _cached_context_group(
        self,
        group: _ContextGroup,
        fallback_policy: tuple[str, ...],
    ) -> _CachedGroupRecognition | None:
        unit_ids = tuple(tile.unit_id for tile in group.tiles)
        cached = tuple(
            self._group_candidate_cache[key]
            for transform in (OcrTransform.RAW, OcrTransform.GAMMA)
            if (
                key := (
                    unit_ids,
                    self.state.locked_profile_id,
                    fallback_policy,
                    transform,
                )
            )
            in self._group_candidate_cache
        )
        if not cached:
            return None
        return max(
            cached,
            key=lambda item: (self._candidate_score(item.candidate) if item.candidate is not None else (-1, -1.0, -1)),
        )

    def _store_context_group(
        self,
        group: _ContextGroup,
        fallback_policy: tuple[str, ...],
        candidate: _Candidate | None,
    ) -> None:
        transform = candidate.transform if candidate is not None else OcrTransform.RAW
        self._group_candidate_cache[
            (
                tuple(tile.unit_id for tile in group.tiles),
                self.state.locked_profile_id,
                fallback_policy,
                transform,
            )
        ] = _CachedGroupRecognition(candidate)

    def _map_context_output(
        self,
        output: OcrEngineOutput,
        placements: tuple[CompactionPlacement, ...],
        original_placements: dict[str, CompactionPlacement],
        full_crop_bbox: Box,
        allowed_unit_ids: tuple[str, ...] = (),
    ) -> OcrEngineOutput:
        mapped_words = []
        allowed = frozenset(allowed_unit_ids)
        for word in output.words:
            overlaps = tuple(
                (
                    self._intersection_area(word.bbox, placement.crop_bbox),
                    placement,
                )
                for placement in placements
                if not allowed or placement.unit_id in allowed
            )
            positive = tuple(item for item in overlaps if item[0] > 0)
            if not positive:
                continue
            _, placement = max(
                positive,
                key=lambda item: (
                    item[0],
                    item[1].unit_id,
                ),
            )
            original = original_placements.get(placement.unit_id)
            if original is None:
                continue
            crop_box = placement.crop_bbox
            clipped = Box(
                max(word.bbox.left, crop_box.left),
                max(word.bbox.top, crop_box.top),
                min(word.bbox.right, crop_box.right),
                min(word.bbox.bottom, crop_box.bottom),
            )
            projected = self._map_word(
                OcrWord(word.text, clipped, word.confidence),
                replace(
                    placement,
                    source_bbox=original.crop_bbox,
                ),
                full_crop_bbox,
            )
            mapped_words.append(projected)
        return OcrEngineOutput(
            text=" ".join(word.text for word in mapped_words),
            words=tuple(mapped_words),
            geometry=output.geometry,
        )

    @staticmethod
    def _source_interval_ppm(box: Box, width: int) -> tuple[int, int]:
        return (
            round(box.left * 1_000_000 / max(1, width)),
            round(box.right * 1_000_000 / max(1, width)),
        )

    @staticmethod
    def _project_box(
        box: Box,
        source: Box,
        target: Box,
    ) -> Box:
        clipped = box.intersection(source)
        if clipped is None:
            raise ValueError("projected OCR box misses its source placement")
        scale_x = target.width / max(1, source.width)
        scale_y = target.height / max(1, source.height)
        left = target.left + round((clipped.left - source.left) * scale_x)
        top = target.top + round((clipped.top - source.top) * scale_y)
        right = target.left + round((clipped.right - source.left) * scale_x)
        bottom = target.top + round((clipped.bottom - source.top) * scale_y)
        left = max(target.left, min(target.right - 1, left))
        top = max(target.top, min(target.bottom - 1, top))
        right = max(left + 1, min(target.right, right))
        bottom = max(top + 1, min(target.bottom, bottom))
        return Box(left, top, right, bottom)

    @staticmethod
    def _topology_slot_by_unit(
        plan: BlockPlan,
        compactions: tuple[BlockCompaction, ...],
    ) -> tuple[dict[str, int], int]:
        ordered = tuple(
            dict.fromkeys(
                tuple(unit.unit_id for unit in plan.membership_units)
                + tuple(placement.unit_id for compaction in compactions for placement in compaction.placements)
            )
        )
        return (
            {unit_id: index for index, unit_id in enumerate(ordered)},
            min((block.bbox.width for block in plan.blocks), default=1),
        )

    def _topology_job_words(
        self,
        *,
        plan: BlockPlan,
        block: RecognitionBlock,
        compaction: BlockCompaction,
        compactions: tuple[BlockCompaction, ...],
        job: OcrJobResult,
    ) -> tuple[tuple[int, OcrWord, SourcePlacementArtifact, Box], ...]:
        if job.output is None or not job.output.words:
            return ()
        artifacts = {item.unit_id: item for item in compaction.source_placements}
        if not artifacts:
            return ()
        associations = []
        if compaction.raster_kind is CompactionRasterKind.CANONICAL_LOCALITY:
            slot_by_unit, slot_width = self._topology_slot_by_unit(
                plan,
                compactions,
            )
            slot_boxes = {
                unit_id: Box(
                    slot % slot_width,
                    slot // slot_width,
                    slot % slot_width + 1,
                    slot // slot_width + 1,
                )
                for unit_id, slot in slot_by_unit.items()
                if unit_id in artifacts
            }
            for index, word in enumerate(job.output.words):
                ranked = tuple(
                    (
                        self._intersection_area(word.bbox, slot_box),
                        unit_id,
                    )
                    for unit_id, slot_box in slot_boxes.items()
                )
                if not ranked:
                    continue
                overlap, unit_id = max(ranked, key=lambda item: (item[0], item[1]))
                if overlap <= 0:
                    continue
                artifact = artifacts[unit_id]
                associations.append((index, word, artifact, artifact.source_bbox))
            return tuple(associations)

        for index, word in enumerate(job.output.words):
            source_box = Box(
                block.bbox.left + word.bbox.left,
                block.bbox.top + word.bbox.top,
                block.bbox.left + word.bbox.right,
                block.bbox.top + word.bbox.bottom,
            )
            ranked = tuple(
                (
                    self._intersection_area(source_box, artifact.source_bbox),
                    artifact,
                )
                for artifact in artifacts.values()
            )
            if not ranked:
                continue
            overlap, artifact = max(
                ranked,
                key=lambda item: (item[0], item[1].unit_id),
            )
            if overlap <= 0:
                continue
            associations.append((index, word, artifact, source_box))
        return tuple(associations)

    def collect_topology_script_evidence(
        self,
        *,
        plan: BlockPlan,
        compactions: tuple[BlockCompaction, ...],
        queue: OcrQueueResult,
    ) -> tuple[TopologyScriptEvidence, ...]:
        jobs = {
            job.block_id: job for job in queue.jobs if job.status is OcrJobStatus.COMPLETE and job.output is not None
        }
        compaction_by_id = {item.block_id: item for item in compactions}
        matrix_sha256 = plan.matrix_sha256 or _cache_fingerprint((plan.aligned_size, plan.source_segment_ids))
        evidence: dict[
            tuple[str, str, str, tuple[int, ...], int, int],
            TopologyScriptEvidence,
        ] = {}
        for block in plan.blocks:
            compaction = compaction_by_id.get(block.block_id)
            job = jobs.get(block.block_id)
            if compaction is None or job is None:
                continue
            for _index, word, artifact, _source_box in self._topology_job_words(
                plan=plan,
                block=block,
                compaction=compaction,
                compactions=compactions,
                job=job,
            ):
                if word.confidence < self._SPECIALIZED_MIN_MEAN_CONFIDENCE:
                    continue
                for script_kind in sorted(self._NATIVE_SCRIPT_EVIDENCE_KINDS):
                    native_text = self._native_word_text(word.text, script_kind)
                    native_characters = sum(
                        self._word_matches_native_script(character, script_kind)
                        for character in native_text
                        if character.isalnum()
                    )
                    if native_characters < self._LOCAL_NATIVE_MIN_CHARACTERS:
                        continue
                    left_ppm, right_ppm = self._source_interval_ppm(
                        artifact.source_bbox,
                        plan.aligned_size[0],
                    )
                    item = TopologyScriptEvidence(
                        script_kind=script_kind,
                        matrix_sha256=matrix_sha256,
                        island_id=artifact.island_id,
                        matrix_columns=(artifact.matrix_column,),
                        source_left_ppm=left_ppm,
                        source_right_ppm=right_ppm,
                        confidence=word.confidence,
                    )
                    key = (
                        item.script_kind,
                        item.matrix_sha256,
                        item.island_id,
                        item.matrix_columns,
                        item.source_left_ppm,
                        item.source_right_ppm,
                    )
                    previous = evidence.get(key)
                    if previous is None or item.confidence > previous.confidence:
                        evidence[key] = item
        return tuple(
            sorted(
                evidence.values(),
                key=lambda item: (
                    item.script_kind,
                    item.matrix_sha256,
                    item.island_id,
                    item.matrix_columns,
                    item.source_left_ppm,
                    item.source_right_ppm,
                ),
            )
        )

    def _topology_route_candidates(
        self,
        *,
        plan: BlockPlan,
        compactions: tuple[BlockCompaction, ...],
        queue: OcrQueueResult,
        evidence: tuple[TopologyScriptEvidence, ...],
    ) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
        jobs = {job.block_id: job for job in queue.jobs}
        compaction_by_id = {item.block_id: item for item in compactions}
        matrix_sha256 = plan.matrix_sha256 or _cache_fingerprint((plan.aligned_size, plan.source_segment_ids))
        all_artifacts = tuple(
            {
                artifact.unit_id: artifact for compaction in compactions for artifact in compaction.source_placements
            }.values()
        )
        routed = []
        for block in plan.blocks:
            compaction = compaction_by_id.get(block.block_id)
            job = jobs.get(block.block_id)
            if compaction is None or job is None or job.status is not OcrJobStatus.COMPLETE or job.output is None:
                continue
            associated = self._topology_job_words(
                plan=plan,
                block=block,
                compaction=compaction,
                compactions=compactions,
                job=job,
            )
            by_unit: dict[str, list[OcrWord]] = {}
            for _index, word, artifact, _source_box in associated:
                by_unit.setdefault(artifact.unit_id, []).append(word)
            for artifact in sorted(
                compaction.source_placements,
                key=lambda item: (item.polar_order, item.unit_id),
            ):
                left_ppm, right_ppm = self._source_interval_ppm(
                    artifact.source_bbox,
                    plan.aligned_size[0],
                )
                same_island = tuple(item for item in all_artifacts if item.island_id == artifact.island_id)
                covered_columns = {
                    item.matrix_column
                    for item in same_island
                    if artifact.source_bbox.left
                    <= (item.source_bbox.left + item.source_bbox.right) // 2
                    < artifact.source_bbox.right
                }
                covered_rows = {
                    item.matrix_row
                    for item in same_island
                    if artifact.source_bbox.top
                    <= (item.source_bbox.top + item.source_bbox.bottom) // 2
                    < artifact.source_bbox.bottom
                }
                spans_topology = len(covered_columns) > 1 or len(covered_rows) > 1
                relevant = tuple(
                    item
                    for item in evidence
                    if (
                        (
                            item.matrix_sha256 == matrix_sha256
                            and item.island_id == artifact.island_id
                            and (
                                spans_topology
                                or min(right_ppm, item.source_right_ppm) > max(left_ppm, item.source_left_ppm)
                            )
                        )
                        or (
                            item.matrix_sha256 != matrix_sha256
                            and min(right_ppm, item.source_right_ppm) > max(left_ppm, item.source_left_ppm)
                        )
                    )
                )
                if not relevant:
                    continue
                words = tuple(by_unit.get(artifact.unit_id, ()))
                covered_scripts = {
                    item.script_kind
                    for item in relevant
                    if sum(
                        self._word_matches_native_script(
                            character,
                            item.script_kind,
                        )
                        for word in words
                        for character in word.text
                        if character.isalnum()
                    )
                    >= self._LOCAL_NATIVE_MIN_CHARACTERS
                }
                missing = tuple(
                    sorted({item.script_kind for item in relevant if item.script_kind not in covered_scripts})
                )
                if not missing:
                    continue
                strictly_covers_interval = any(
                    left_ppm <= item.source_left_ppm
                    and right_ppm >= item.source_right_ppm
                    and (right_ppm - left_ppm) > (item.source_right_ppm - item.source_left_ppm) * 5 // 4
                    for item in relevant
                )
                if not spans_topology and not strictly_covers_interval:
                    continue
                routed.append(
                    (
                        block.block_id,
                        artifact.unit_id,
                        missing or tuple(sorted({item.script_kind for item in relevant})),
                    )
                )
        return tuple(routed)

    def _direct_source_placement_group(
        self,
        *,
        block_id: str,
        artifact: SourcePlacementArtifact,
        first_order: int,
    ) -> _ContextGroup | None:
        pixels = self._decode_source_placement(artifact)
        if pixels is None:
            return None
        crop_bbox = Box(0, 0, artifact.source_bbox.width, artifact.source_bbox.height)
        return _ContextGroup(
            group_id=f"{block_id}:topology-source:{artifact.unit_id}",
            depth=0,
            first_order=first_order,
            tiles=(
                _Tile(
                    unit_id=artifact.unit_id,
                    segment_ids=artifact.segment_ids,
                    source_bbox=artifact.source_bbox,
                    pixels=pixels,
                ),
            ),
            png_bytes=artifact.png_bytes,
            placements=(
                CompactionPlacement(
                    unit_id=artifact.unit_id,
                    segment_ids=artifact.segment_ids,
                    source_bbox=artifact.source_bbox,
                    crop_bbox=crop_bbox,
                ),
            ),
            emit_unit_ids=(artifact.unit_id,),
            source_fallback=True,
        )

    def _fuse_topology_source_words(
        self,
        *,
        anchor_bbox: Box,
        combined: _Candidate,
        native_candidates: tuple[tuple[str, _Candidate], ...],
    ) -> tuple[tuple[OcrWord, str, _Candidate], ...] | None:
        base = [
            (word, "combined", combined)
            for word in combined.output.words
            if self._intersection_area(word.bbox, anchor_bbox) > 0
        ]
        if not base:
            return None
        replacements: dict[int, tuple[tuple[OcrWord, str, _Candidate], ...]] = {}
        removed: set[int] = set()
        for script_kind, candidate in native_candidates:
            runs: list[list[OcrWord]] = []
            current: list[OcrWord] = []
            for word in candidate.output.words:
                native_text = self._native_word_text(word.text, script_kind)
                if (
                    native_text
                    and word.confidence >= self._SPECIALIZED_MIN_MEAN_CONFIDENCE
                    and self._intersection_area(word.bbox, anchor_bbox) > 0
                ):
                    current.append(word)
                elif current:
                    runs.append(current)
                    current = []
            if current:
                runs.append(current)
            for run in runs:
                native_characters = sum(
                    self._word_matches_native_script(character, script_kind)
                    for word in run
                    for character in word.text
                    if character.isalnum()
                )
                if native_characters < self._LOCAL_NATIVE_MIN_CHARACTERS:
                    continue
                matched = []
                for index, (base_word, _kind, _candidate) in enumerate(base):
                    if index in removed or not any(character.isalnum() for character in base_word.text):
                        continue
                    for native_word in run:
                        horizontal = max(
                            0,
                            min(base_word.bbox.right, native_word.bbox.right)
                            - max(base_word.bbox.left, native_word.bbox.left),
                        )
                        vertical = max(
                            0,
                            min(base_word.bbox.bottom, native_word.bbox.bottom)
                            - max(base_word.bbox.top, native_word.bbox.top),
                        )
                        if (
                            horizontal / max(1, base_word.bbox.width) >= 0.5
                            and vertical / max(1, base_word.bbox.height) >= 0.35
                        ):
                            matched.append(index)
                            break
                if not matched:
                    continue
                first = min(matched)
                removed.update(matched)
                replacements[first] = tuple((word, script_kind, candidate) for word in run)
        if not replacements:
            return None
        fused = []
        seen = set()
        for index, item in enumerate(base):
            emitted = replacements.get(index, ())
            if index not in removed:
                emitted = (*emitted, item)
            for span in emitted:
                word = span[0]
                key = (word.text, word.bbox, round(word.confidence, 6))
                if key in seen:
                    continue
                seen.add(key)
                fused.append(span)
        return tuple(fused)

    def apply_topology_source_fusion(
        self,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        compactions: tuple[BlockCompaction, ...],
        queue: OcrQueueResult,
        evidence: tuple[TopologyScriptEvidence, ...],
    ) -> TopologySourceFusionResult:
        started = time.perf_counter()
        cache_before = self._content_cache.snapshot()
        routes = self._topology_route_candidates(
            plan=plan,
            compactions=compactions,
            queue=queue,
            evidence=evidence,
        )
        if not routes:
            return TopologySourceFusionResult(
                queue=queue,
                changed_jobs=(),
                provenance=(),
                elapsed_seconds=time.perf_counter() - started,
                cache_metrics={},
            )
        blocks = {item.block_id: item for item in plan.blocks}
        compaction_by_id = {item.block_id: item for item in compactions}
        jobs = list(queue.jobs)
        provenance = []
        changed_jobs = []
        route_by_block: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
        for block_id, unit_id, script_kinds in routes:
            route_by_block.setdefault(block_id, []).append((unit_id, script_kinds))
        for job_index, job in enumerate(jobs):
            block_routes = route_by_block.get(job.block_id)
            if not block_routes or job.output is None:
                continue
            block = blocks[job.block_id]
            compaction = compaction_by_id[job.block_id]
            artifacts = {item.unit_id: item for item in compaction.source_placements}
            current_job = job
            for order, (unit_id, script_kinds) in enumerate(block_routes):
                artifact = artifacts.get(unit_id)
                if artifact is None:
                    continue
                group = self._direct_source_placement_group(
                    block_id=job.block_id,
                    artifact=artifact,
                    first_order=order,
                )
                if group is None:
                    continue
                combined_profile_id = (
                    job.lane_id
                    if self._specialized_profile_kind(job.lane_id, ()) is None
                    else self.state.locked_profile_id or self._DEFAULT_PROFILE_ID
                )
                combined = self._source_line_candidate(
                    block_id=job.block_id,
                    group=group,
                    fallback_policy=(combined_profile_id,),
                    script_kinds=frozenset(),
                    require_native_coverage=False,
                )
                if combined is None:
                    continue
                native_candidates = []
                for script_kind in script_kinds:
                    profile_ids = tuple(
                        profile_id
                        for profile_id in self.state.ordered_profile_ids()
                        if self._specialized_profile_kind(
                            profile_id,
                            next(
                                (profile.languages for profile in self._profiles if profile.profile_id == profile_id),
                                (),
                            ),
                        )
                        == script_kind
                    )
                    if not profile_ids:
                        continue
                    native = self._source_line_candidate(
                        block_id=job.block_id,
                        group=group,
                        fallback_policy=profile_ids,
                        script_kinds=frozenset((script_kind,)),
                    )
                    if native is not None:
                        native_candidates.append((script_kind, native))
                if not native_candidates:
                    continue
                route_elapsed = combined.elapsed_seconds + sum(
                    candidate.elapsed_seconds for _script_kind, candidate in native_candidates
                )
                anchor_bbox = group.placements[0].crop_bbox
                fused = self._fuse_topology_source_words(
                    anchor_bbox=anchor_bbox,
                    combined=combined,
                    native_candidates=tuple(native_candidates),
                )
                if fused is None:
                    continue
                associations = self._topology_job_words(
                    plan=plan,
                    block=block,
                    compaction=compaction,
                    compactions=compactions,
                    job=current_job,
                )
                replaced_indexes = {
                    index for index, _word, associated, _source_box in associations if associated.unit_id == unit_id
                }
                if not replaced_indexes:
                    continue
                first_replaced = min(replaced_indexes)
                mapped_spans = []
                for word, script_kind, candidate in fused:
                    source_box = self._project_box(
                        word.bbox,
                        anchor_bbox,
                        artifact.source_bbox,
                    )
                    if compaction.raster_kind is CompactionRasterKind.CANONICAL_LOCALITY:
                        original_word = current_job.output.words[first_replaced]
                        output_box = original_word.bbox
                    else:
                        output_box = Box(
                            max(0, source_box.left - block.bbox.left),
                            max(0, source_box.top - block.bbox.top),
                            min(block.bbox.width, source_box.right - block.bbox.left),
                            min(block.bbox.height, source_box.bottom - block.bbox.top),
                        )
                    mapped_word = OcrWord(word.text, output_box, word.confidence)
                    mapped_spans.append(mapped_word)
                    provenance.append(
                        TopologyFusionSpanProvenance(
                            block_id=job.block_id,
                            unit_id=unit_id,
                            script_kind=script_kind,
                            text=word.text,
                            source_bbox=source_box,
                            output_bbox=output_box,
                            profile_id=candidate.profile.profile_id,
                            transform=OcrTransform.SOURCE_PLACEMENT_FALLBACK,
                            input_sha256=candidate.input_sha256,
                        )
                    )
                output_words = []
                inserted = False
                for index, word in enumerate(current_job.output.words):
                    if index == first_replaced:
                        output_words.extend(mapped_spans)
                        inserted = True
                    if index not in replaced_indexes:
                        output_words.append(word)
                if not inserted:
                    continue
                deduplicated = []
                seen = set()
                for word in output_words:
                    key = (word.text, word.bbox, round(word.confidence, 6))
                    if key in seen:
                        continue
                    seen.add(key)
                    deduplicated.append(word)
                output = OcrEngineOutput(
                    text=" ".join(word.text for word in deduplicated),
                    words=tuple(deduplicated),
                    geometry=OcrOutputGeometry.WORD_BOXES,
                )
                if output.text == current_job.output.text:
                    continue
                current_job = replace(
                    current_job,
                    transform=OcrTransform.CONTEXTUAL_COMPOSITE,
                    output=output,
                    elapsed_seconds=current_job.elapsed_seconds + route_elapsed,
                    input_sha256=current_job.context_sha256,
                )
            if current_job is not job:
                jobs[job_index] = current_job
                changed_jobs.append(job.job_id)
        cache_after = self._content_cache.snapshot()
        cache_metrics = {
            "requests": cache_after.requests - cache_before.requests,
            "hits": cache_after.hits - cache_before.hits,
            "misses": cache_after.misses - cache_before.misses,
            "exact_duplicate_calls_avoided": cache_after.hits - cache_before.hits,
            "ocr_work_seconds": (cache_after.ocr_work_seconds - cache_before.ocr_work_seconds),
        }
        diagnostics = list(queue.diagnostics)
        for item in provenance:
            diagnostics.append(
                "topology-fusion-span="
                f"block={item.block_id};unit={item.unit_id};"
                f"script={item.script_kind};profile={item.profile_id};"
                f"transform={item.transform.value};input={item.input_sha256};"
                f"source_bbox={item.source_bbox.left},{item.source_bbox.top},"
                f"{item.source_bbox.right},{item.source_bbox.bottom};"
                f"output_bbox={item.output_bbox.left},{item.output_bbox.top},"
                f"{item.output_bbox.right},{item.output_bbox.bottom}"
            )
        if self.log_path is not None:
            self.state.write_csv(self.log_path)
        return TopologySourceFusionResult(
            queue=replace(queue, jobs=tuple(jobs), diagnostics=tuple(diagnostics)),
            changed_jobs=tuple(changed_jobs),
            provenance=tuple(provenance),
            elapsed_seconds=time.perf_counter() - started,
            cache_metrics=cache_metrics,
        )

    def _recognize_context_groups(
        self,
        *,
        block_id: str,
        block_bbox: Box,
        groups: tuple[_ContextGroup, ...],
        fallback_profile_ids: tuple[str, ...],
        forced_native_search_group_ids: frozenset[str] = frozenset(),
        routed_native_unit_ids: frozenset[str] = frozenset(),
        source_placements: tuple[SourcePlacementArtifact, ...] = (),
    ) -> tuple[tuple[_ContextGroup, _Candidate], ...]:
        if not groups:
            return ()
        nodes = tuple(replace(node) for node in self.state.nodes)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = tuple(
                executor.submit(
                    self._recognize_isolated,
                    block_id=block_id,
                    unit_id=group.group_id,
                    raw_png=group.png_bytes,
                    nodes=nodes,
                    locked_profile_id=self.state.locked_profile_id,
                    mixed_fallback_only=False,
                    fallback_profile_ids=(),
                    run_primary=True,
                )
                for group in groups
            )
            isolated = tuple(future.result() for future in futures)

        for item in isolated:
            self.state.merge_attempts(item.attempts)
            for profile_id, grammar_percent in item.observations:
                self.state.observe(profile_id, grammar_percent)

        accepted = []
        children = []
        forced_children: set[str] = set()
        for group, primary in zip(groups, isolated):
            output_group = group
            group_has_routed_native_unit = not routed_native_unit_ids or any(
                tile.unit_id in routed_native_unit_ids for tile in group.tiles
            )
            forced_native_search = group.group_id in forced_native_search_group_ids and group_has_routed_native_unit
            local_native_evidence = False
            fallback_policy = self._ordered_context_fallback_policy(fallback_profile_ids)
            cached = self._cached_context_group(group, fallback_policy)
            missing_native_script: bool | None = None
            if cached is not None:
                candidate = cached.candidate
            else:
                candidate = primary.candidate
                primary_missing_native_script = self._missing_evidenced_native_script(
                    candidate,
                    fallback_profile_ids,
                )
                if fallback_policy and (
                    candidate is None
                    or candidate.assessment.percent < self._LOCK_GRAMMAR_PERCENT
                    or forced_native_search
                ):
                    fallback = self._recognize_image(
                        block_id=block_id,
                        unit_id=group.group_id,
                        raw_png=group.png_bytes,
                        primary_profile_id=self.state.locked_profile_id,
                        transforms=(OcrTransform.RAW,),
                        mixed_fallback_only=False,
                        fallback_profile_ids=fallback_policy,
                        run_primary=False,
                    ).candidate
                    evidenced_script_kinds = self._fallback_script_kinds(fallback_profile_ids)
                    locally_evidenced_scripts = tuple(
                        script_kind
                        for script_kind in evidenced_script_kinds
                        if self._candidate_has_local_native_evidence(
                            fallback,
                            script_kind,
                            leaf=len(group.tiles) == 1,
                        )
                    )
                    local_native_evidence = bool(locally_evidenced_scripts)
                    native_patch = (
                        self._patch_missing_native_units(
                            candidate,
                            fallback,
                            group.placements,
                            fallback_profile_ids,
                        )
                        if local_native_evidence
                        else None
                    )
                    if native_patch is not None:
                        candidate = native_patch
                    local_missing_native_script = primary_missing_native_script and (
                        bool(locally_evidenced_scripts) or forced_native_search
                    )
                    if native_patch is not None:
                        missing_native_script = False
                    elif local_missing_native_script:
                        if fallback is not None and len(group.tiles) == 1 and bool(locally_evidenced_scripts):
                            candidate = fallback
                            missing_native_script = False
                        elif len(group.tiles) == 1:
                            source_group = self._build_source_placement_line(
                                block_id=block_id,
                                leaf_group=group,
                                source_placements=source_placements,
                            )
                            if source_group is not None:
                                source_candidate = self._source_line_candidate(
                                    block_id=block_id,
                                    group=source_group,
                                    fallback_policy=fallback_policy,
                                    script_kinds=evidenced_script_kinds,
                                )
                                if source_candidate is not None:
                                    candidate = source_candidate
                                    output_group = source_group
                            missing_native_script = False
                        else:
                            missing_native_script = True
                    elif fallback is not None and (
                        not primary_missing_native_script
                        and (candidate is None or self._candidate_score(fallback) > self._candidate_score(candidate))
                    ):
                        candidate = fallback

                if missing_native_script is None:
                    missing_native_script = False
                if output_group is group:
                    self._store_context_group(
                        group,
                        fallback_policy,
                        candidate,
                    )

            if missing_native_script is None:
                missing_native_script = self._missing_evidenced_native_script(
                    candidate,
                    fallback_profile_ids,
                )
            requires_descent = self._candidate_requires_context_recursion(
                candidate,
                fallback_profile_ids,
                missing_native_script=missing_native_script,
            )
            if requires_descent and len(group.tiles) > 1:
                if len(group.tiles) > 16:
                    next_limit = 16
                elif missing_native_script:
                    next_limit = 4 if len(group.tiles) > 4 else 1
                else:
                    next_limit = 0
                if next_limit == 0:
                    if candidate is not None:
                        accepted.append((group, candidate))
                    continue
                for offset in range(0, len(group.tiles), next_limit):
                    child_tiles = group.tiles[offset : offset + next_limit]
                    child = self._build_context_group(
                        block_id=block_id,
                        depth=group.depth + 1,
                        first_order=group.first_order + offset,
                        tiles=child_tiles,
                    )
                    children.append(child)
                    if missing_native_script:
                        if (
                            local_native_evidence
                            or not routed_native_unit_ids
                            or any(tile.unit_id in routed_native_unit_ids for tile in child.tiles)
                        ):
                            forced_children.add(child.group_id)
                continue
            if candidate is not None:
                accepted.append((output_group, candidate))

        if children:
            accepted.extend(
                self._recognize_context_groups(
                    block_id=block_id,
                    block_bbox=block_bbox,
                    groups=tuple(children),
                    fallback_profile_ids=fallback_profile_ids,
                    forced_native_search_group_ids=frozenset(forced_children),
                    routed_native_unit_ids=routed_native_unit_ids,
                    source_placements=source_placements,
                )
            )
        return tuple(
            sorted(
                accepted,
                key=lambda item: item[0].first_order,
            )
        )

    def _membership_recursive_candidate(
        self,
        *,
        block_id: str,
        block_bbox: Box,
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
                    payload_buffer = io.BytesIO()
                    tile.save(payload_buffer, format="PNG", compress_level=1)
                    tile_png = payload_buffer.getvalue()
                finally:
                    tile.close()
                cache_key = (
                    hashlib.sha256(tile_png).hexdigest(),
                    placement.segment_ids,
                    placement.source_bbox.as_tuple(),
                )
                cached = self._unit_candidate_cache.get(cache_key)
                if cached is None:
                    decision = self._recognize_image(
                        block_id=block_id,
                        unit_id=placement.unit_id,
                        raw_png=tile_png,
                        primary_profile_id=self.state.locked_profile_id,
                        transforms=(OcrTransform.RAW,),
                        mixed_fallback_only=True,
                    )
                    candidate = decision.candidate
                    cached = _CachedUnitRecognition(
                        candidate=candidate,
                        crop_size=(
                            placement.crop_bbox.width,
                            placement.crop_bbox.height,
                        ),
                    )
                    self._unit_candidate_cache[cache_key] = cached
                else:
                    candidate = cached.candidate
                if candidate is None:
                    continue
                local_placement = replace(
                    placement,
                    crop_bbox=Box(
                        0,
                        0,
                        cached.crop_size[0],
                        cached.crop_size[1],
                    ),
                )
                mapped = tuple(self._map_word(word, local_placement, block_bbox) for word in candidate.output.words)
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
        words = tuple(word for _, _, mapped in ordered_candidates for word in mapped)
        if not words:
            return None
        total_characters = sum(max(1, len(candidate.output.text)) for _, candidate, _ in candidates)
        grammar_percent = round(
            sum(candidate.assessment.percent * max(1, len(candidate.output.text)) for _, candidate, _ in candidates)
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
            elapsed_seconds=sum(candidate.elapsed_seconds for _, candidate, _ in candidates),
        )

    def _recursive_candidate(
        self,
        *,
        block_id: str,
        block_bbox: Box,
        crop: BlockCropPair,
        compaction: BlockCompaction,
        fallback_profile_ids: tuple[str, ...],
        native_script_unit_ids: frozenset[str] = frozenset(),
    ) -> _Candidate | None:
        with Image.open(io.BytesIO(crop.raw.png_bytes)) as opened:
            opened.load()
            image = opened.convert("RGB")
        full_crop_bbox = Box(0, 0, image.width, image.height)
        try:
            ordered_placements = tuple(
                sorted(
                    compaction.placements,
                    key=lambda placement: (
                        placement.source_bbox.top,
                        placement.source_bbox.left,
                        placement.source_bbox.bottom,
                        placement.source_bbox.right,
                        placement.unit_id,
                    ),
                )
            )
            tiles = []
            for placement in ordered_placements:
                tile_image = image.crop(placement.crop_bbox.as_tuple())
                try:
                    pixels = np.asarray(tile_image).copy()
                finally:
                    tile_image.close()
                tiles.append(
                    _Tile(
                        unit_id=placement.unit_id,
                        segment_ids=placement.segment_ids,
                        source_bbox=placement.source_bbox,
                        pixels=pixels,
                        canonical_bbox=(
                            placement.crop_bbox
                            if getattr(
                                compaction,
                                "raster_kind",
                                None,
                            )
                            is CompactionRasterKind.CANONICAL_LOCALITY
                            else None
                        ),
                        canonical_png_bytes=(
                            crop.raw.png_bytes
                            if getattr(
                                compaction,
                                "raster_kind",
                                None,
                            )
                            is CompactionRasterKind.CANONICAL_LOCALITY
                            else None
                        ),
                    )
                )
        finally:
            image.close()
        if not tiles:
            return None

        initial_groups = tuple(
            self._build_context_group(
                block_id=block_id,
                depth=0,
                first_order=offset,
                tiles=tuple(tiles[offset : offset + 64]),
            )
            for offset in range(0, len(tiles), 64)
        )
        recognized = self._recognize_context_groups(
            block_id=block_id,
            block_bbox=block_bbox,
            groups=initial_groups,
            fallback_profile_ids=fallback_profile_ids,
            forced_native_search_group_ids=frozenset(
                group.group_id
                for group in initial_groups
                if not native_script_unit_ids or any(tile.unit_id in native_script_unit_ids for tile in group.tiles)
            ),
            routed_native_unit_ids=native_script_unit_ids,
            source_placements=compaction.source_placements,
        )
        original_placements = {placement.unit_id: placement for placement in compaction.placements}
        candidates = []
        for group, candidate in recognized:
            mapped = self._map_context_output(
                candidate.output,
                group.placements,
                original_placements,
                full_crop_bbox,
                allowed_unit_ids=group.emit_unit_ids,
            )
            if mapped.words:
                candidates.append((group, candidate, mapped))
        if not candidates:
            return None

        words = tuple(word for _, _, output in candidates for word in output.words)
        total_characters = sum(max(1, len(output.text)) for _, _, output in candidates)
        grammar_percent = round(
            sum(candidate.assessment.percent * max(1, len(output.text)) for _, candidate, output in candidates)
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
            transform=OcrTransform.CONTEXTUAL_COMPOSITE,
            output=output,
            assessment=GrammarAssessment(
                grammar_percent,
                grammar_percent == 100,
                ("recursive-context-groups",),
            ),
            input_sha256=hashlib.sha256(crop.raw.png_bytes).hexdigest(),
            elapsed_seconds=sum(candidate.elapsed_seconds for _, candidate, _ in candidates),
        )

    @staticmethod
    def _observed_scripts(text: str) -> frozenset[str]:
        return frozenset(script for character in text if (script := _character_script(character)) != "neutral")

    def _recognize_recursive_blocks(
        self,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        compaction_by_id: dict[str, BlockCompaction],
        recognized: tuple[_FullBlockRecognition, ...],
        allow_membership_calibration: bool,
    ) -> dict[int, _Candidate | None]:
        pending = []
        for index, (block, crop, full_block) in enumerate(zip(plan.blocks, crops, recognized)):
            compaction = compaction_by_id.get(block.block_id)
            if (
                compaction is None
                or not 1 < len(compaction.placements) <= 16 * 16
                or not self._should_recurse_full_block(
                    full_block,
                    allow_membership_calibration=(allow_membership_calibration and index == 0),
                )
            ):
                continue
            pending.append((index, block, crop, compaction, full_block))
        if not pending:
            return {}

        results: dict[int, _Candidate | None] = {}

        def recognize_batch(
            entries: tuple[tuple[object, ...], ...],
        ) -> None:
            if not entries:
                return
            nodes = tuple(replace(node) for node in self.state.nodes)
            with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers) as executor:
                futures = tuple(
                    (
                        int(index),
                        executor.submit(
                            self._recognize_recursive_isolated,
                            block_id=block.block_id,
                            block_bbox=block.bbox,
                            crop=crop,
                            compaction=compaction,
                            fallback_profile_ids=(full_block.fallback_profile_ids),
                            native_script_unit_ids=frozenset(full_block.native_script_unit_ids),
                            nodes=nodes,
                            locked_profile_id=self.state.locked_profile_id,
                            lock_is_provisional=(self.state.lock_is_provisional),
                            contextual=(self._uses_specialized_context_recursion(full_block)),
                        ),
                    )
                    for index, block, crop, compaction, full_block in entries
                )
                isolated = tuple((index, future.result()) for index, future in futures)

            # Futures may finish in any order; state and diagnostics preserve
            # the original block order.
            for index, item in isolated:
                self.state.merge_attempts(item.attempts)
                for profile_id, grammar_percent in item.observations:
                    self.state.observe(profile_id, grammar_percent)
                results[index] = item.candidate

        calibration_by_key: dict[
            tuple[str | None, tuple[str, ...]],
            list[tuple[object, ...]],
        ] = {}
        key_by_index: dict[
            int,
            tuple[str | None, tuple[str, ...]],
        ] = {}
        for entry in pending:
            index, block, _crop, _compaction, full_block = entry
            key = self._local_split_strategy_key(block, full_block)
            if key is None:
                continue
            key_by_index[index] = key
            samples = calibration_by_key.setdefault(key, [])
            if len(samples) < self._SPLIT_CALIBRATION_SAMPLES:
                samples.append(entry)

        calibration_entries = tuple(entry for samples in calibration_by_key.values() for entry in samples)
        if not calibration_entries:
            recognize_batch(tuple(pending))
            return results

        # Process a contiguous prefix so attempt/splay order remains identical
        # to the source block order even when evidence keys are interleaved.
        calibration_frontier = max(int(entry[0]) for entry in calibration_entries)
        first_batch = tuple(entry for entry in pending if int(entry[0]) <= calibration_frontier)
        recognize_batch(first_batch)

        ineffective_keys = set()
        for key, samples in calibration_by_key.items():
            if len(samples) < self._SPLIT_CALIBRATION_SAMPLES:
                continue
            successes = sum(
                self._recursive_calibration_succeeded(
                    results.get(int(entry[0])),
                    entry[4],
                )
                for entry in samples
            )
            if successes < self._SPLIT_CALIBRATION_MIN_SUCCESSES:
                ineffective_keys.add(key)

        remaining = tuple(
            entry
            for entry in pending
            if int(entry[0]) > calibration_frontier and key_by_index.get(int(entry[0])) not in ineffective_keys
        )
        recognize_batch(remaining)
        return results

    @staticmethod
    def _validate_compaction_contract(
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        compactions: tuple[BlockCompaction, ...],
    ) -> None:
        compaction_by_id = {compaction.block_id: compaction for compaction in compactions}
        for block, crop in zip(plan.blocks, crops):
            if block.matrix_window_kind not in {
                "polar-local-full",
                "polar-local-signature",
            }:
                continue
            compaction = compaction_by_id.get(block.block_id)
            if compaction is None:
                raise ValueError("canonical locality block has no compaction contract")
            if getattr(compaction, "raster_kind", None) is not CompactionRasterKind.CANONICAL_LOCALITY:
                raise ValueError("locality block was not produced by canonical renderer")
            actual_sha256 = hashlib.sha256(crop.raw.png_bytes).hexdigest()
            if getattr(compaction, "raw_sha256", "") != actual_sha256:
                raise ValueError("canonical locality raster digest disagrees with crop")
            for placement in compaction.placements:
                if (
                    placement.crop_bbox.left < 0
                    or placement.crop_bbox.top < 0
                    or placement.crop_bbox.right > crop.bbox.width
                    or placement.crop_bbox.bottom > crop.bbox.height
                ):
                    raise ValueError("canonical locality placement exceeds raster bounds")

    def run_with_compaction(
        self,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
        compactions: tuple[BlockCompaction, ...],
    ) -> OcrQueueResult:
        if self._closed:
            raise RuntimeError("adaptive OCR session is closed")
        self._validate_compaction_contract(
            plan=plan,
            crops=crops,
            compactions=compactions,
        )
        cache_before = self._content_cache.snapshot()
        compaction_by_id = {item.block_id: item for item in compactions}
        canonical_contract_by_block_id = self._canonical_membership_contract(
            plan,
            crops,
            compactions,
        )
        jobs = []
        diagnostics = []
        allow_membership_calibration = self.state.locked_profile_id is None
        recognized = self._recognize_blocks(
            plan=plan,
            crops=crops,
            compaction_by_id=compaction_by_id,
        )
        recursive_by_index = self._recognize_recursive_blocks(
            plan=plan,
            crops=crops,
            compaction_by_id=compaction_by_id,
            recognized=recognized,
            allow_membership_calibration=allow_membership_calibration,
        )
        if self.state.locked_profile_id is not None:
            diagnostics.append(
                "document-language-lock="
                f"{self.state.locked_profile_id};kind="
                f"{'provisional' if self.state.lock_is_provisional else 'confirmed'}"
            )
        for index, (block, crop) in enumerate(zip(plan.blocks, crops)):
            full_block = recognized[index]
            candidate = full_block.candidate
            recognition_seconds = full_block.elapsed_seconds
            recursive_started = time.perf_counter()
            compaction = compaction_by_id.get(block.block_id)
            should_recurse = (
                compaction is not None
                and 1 < len(compaction.placements) <= 16 * 16
                and self._should_recurse_full_block(
                    full_block,
                    allow_membership_calibration=(allow_membership_calibration and index == 0),
                )
            )
            if should_recurse:
                assert compaction is not None
                recursive = recursive_by_index.get(index)
                if recursive is not None:
                    if not self._uses_specialized_context_recursion(full_block):
                        if candidate is None or recursive.assessment.percent >= candidate.assessment.percent:
                            candidate = recursive
                            diagnostics.append(f"block={block.block_id};" "recursive=membership-units")
                        recursive = None
                if recursive is not None:
                    native_patch = self._patch_missing_native_units(
                        candidate,
                        recursive,
                        compaction.placements,
                        full_block.fallback_profile_ids,
                    )
                    if native_patch is not None:
                        candidate = native_patch
                        diagnostics.append(f"block={block.block_id};" "recursive=native-unit-patch")
                    else:
                        if full_block.native_script_runs:
                            recursive = replace(
                                recursive,
                                output=self._reconcile_native_runs(
                                    recursive.output,
                                    full_block.native_script_runs,
                                ),
                            )
                    if native_patch is None and not self._contextual_composite_is_acceptable(
                        recursive,
                        full_block.fallback_profile_ids,
                    ):
                        diagnostics.append(
                            f"block={block.block_id};"
                            "recursive=contextual-composite-rejected;"
                            f"grammar={recursive.assessment.percent};"
                            "confidence="
                            f"{self._mean_confidence(recursive.output):.6f}"
                        )
                    elif native_patch is None and self._recursive_calibration_succeeded(
                        recursive,
                        full_block,
                    ):
                        candidate = recursive
                        diagnostics.append(f"block={block.block_id};" "recursive=contextual-composite")
            if candidate is None:
                input_sha256 = hashlib.sha256(crop.raw.png_bytes).hexdigest()
                diagnostics.append(f"block={block.block_id};outcome=unresolved;" "reason=profile-exhausted")
                jobs.append(
                    OcrJobResult(
                        job_id=f"ocr-job-{index:08d}",
                        block_id=block.block_id,
                        transform=OcrTransform.RAW,
                        lane_id="adaptive-language",
                        resource=OcrResource.CPU,
                        status=OcrJobStatus.COMPLETE,
                        output=OcrEngineOutput(
                            text="",
                            words=(),
                            geometry=OcrOutputGeometry.WORD_BOXES,
                        ),
                        error_type=None,
                        error_message=None,
                        elapsed_seconds=(recognition_seconds + time.perf_counter() - recursive_started),
                        input_sha256=input_sha256,
                        context_sha256=input_sha256,
                        failure_code=None,
                        capability_id="adaptive-language-unresolved",
                    )
                )
                continue
            canonical_locality = (
                compaction is not None
                and getattr(compaction, "raster_kind", None) is CompactionRasterKind.CANONICAL_LOCALITY
            )
            if canonical_locality:
                diagnostics.append(f"block={block.block_id};" "geometry=canonical-membership-slots-v1")
                try:
                    (
                        membership_slot_by_id,
                        canonical_slot_size,
                    ) = canonical_contract_by_block_id[block.block_id]
                except KeyError as error:
                    raise ValueError("canonical locality block has no membership contract") from error
            else:
                membership_slot_by_id = {}
                canonical_slot_size = (0, 0)
            mapped_output = (
                self._map_full_output(
                    candidate.output,
                    compaction,
                    block.bbox,
                    membership_slot_by_id,
                    canonical_slot_size,
                )
                if canonical_locality or candidate.transform is not OcrTransform.CONTEXTUAL_COMPOSITE
                else candidate.output
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
                    elapsed_seconds=(recognition_seconds + time.perf_counter() - recursive_started),
                    input_sha256=candidate.input_sha256,
                    context_sha256=hashlib.sha256(crop.raw.png_bytes).hexdigest(),
                    failure_code=None,
                    capability_id=candidate.profile.profile_id,
                )
            )
            diagnostics.append(
                f"block={block.block_id};profile={candidate.profile.profile_id};"
                f"grammar={candidate.assessment.percent};"
                f"transform={candidate.transform.value};"
                f"cache={'hit' if candidate.cache_hit else 'miss'}"
            )
        cache_after = self._content_cache.snapshot()
        diagnostics.extend(
            (
                f"content-cache.requests=" f"{cache_after.requests - cache_before.requests}",
                f"content-cache.hits={cache_after.hits - cache_before.hits}",
                f"content-cache.misses=" f"{cache_after.misses - cache_before.misses}",
                f"content-cache.exact-duplicate-calls-avoided=" f"{cache_after.hits - cache_before.hits}",
                f"content-cache.ocr-work-seconds="
                f"{cache_after.ocr_work_seconds - cache_before.ocr_work_seconds:.6f}",
            )
        )
        if self.log_path is not None:
            self.state.write_csv(self.log_path)
        complete = sum(job.status is OcrJobStatus.COMPLETE for job in jobs)
        failed = len(jobs) - complete
        return OcrQueueResult(
            jobs=tuple(jobs),
            status=(OcrQueueStatus.COMPLETE if failed == 0 else OcrQueueStatus.PARTIAL),
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
    "AdaptiveOcrContentCache",
    "AdaptivePersistentOcrSession",
    "BlockCompaction",
    "CanonicalLocalityRaster",
    "CompactionRasterKind",
    "CompactionPlacement",
    "GrammarAssessment",
    "LanguageProfile",
    "LanguageSplayState",
    "OcrContentCacheKey",
    "OcrContentCacheStats",
    "assess_grammar",
    "build_deferred_compact_crops",
]
