from __future__ import annotations

import io
import hashlib
import concurrent.futures
import re
import threading
from dataclasses import dataclass, replace
from types import SimpleNamespace

import numpy as np
from PIL import Image

from app.sparse_pipeline import adaptive_language_ocr as adaptive
from app.sparse_pipeline.adaptive_language_ocr import (
    AdaptivePersistentOcrSession,
    BlockCompaction,
    CompactionPlacement,
    GrammarAssessment,
)
from app.sparse_pipeline.block_crops import BlockCropPair
from app.sparse_pipeline.block_planning import (
    BlockSetAlgebra,
    BlockPlan,
    MatrixLocalIsland,
    MatrixLocalPlacement,
    RecognitionBlock,
)
from app.sparse_pipeline.contracts import Box
from app.sparse_pipeline.crop_enhancement import CropInput
from app.sparse_pipeline.ocr_adapter_contracts import (
    OcrOutputGeometry,
    OcrResource,
)
from app.sparse_pipeline.ocr_queue import (
    OcrEngineOutput,
    OcrJobStatus,
    OcrLane,
    OcrQueueStatus,
    OcrWord,
)


@dataclass(frozen=True)
class _Config:
    languages: tuple[str, ...]


class _Worker:
    scores: dict[tuple[str, tuple[int, int], int], int] = {}
    texts: dict[tuple[str, tuple[int, int], int], str] = {}
    calls: list[tuple[str, tuple[int, int], int]] = []
    call_thread_ids: list[tuple[tuple[str, tuple[int, int], int], int]] = []
    barrier: threading.Barrier | None = None
    barrier_keys: set[tuple[str, tuple[int, int], int]] = set()
    barrier_passes = 0
    state_lock = threading.Lock()
    score_hook: object | None = None
    text_hook: object | None = None
    confidence = 0.99

    def __init__(self, *, config: _Config) -> None:
        self.config = config

    def recognize(self, png_bytes: bytes) -> OcrEngineOutput:
        with Image.open(io.BytesIO(png_bytes)) as opened:
            opened.load()
            size = opened.size
            label = opened.convert("RGB").getpixel((0, 0))[0]
        profile_id = "-".join(self.config.languages)
        key = (profile_id, size, label)
        self.calls.append(key)
        self.call_thread_ids.append((key, threading.get_ident()))
        if self.barrier is not None and key in self.barrier_keys:
            self.barrier.wait(timeout=2.0)
            with self.state_lock:
                type(self).barrier_passes += 1
        score = type(self).score_hook(key, png_bytes) if type(self).score_hook is not None else self.scores.get(key, 10)
        text = (
            type(self).text_hook(key, png_bytes)
            if type(self).text_hook is not None
            else self.texts.get(
                key,
                (
                    f"中文score{score}"
                    if key
                    in {
                        ("rus-eng", (10, 10), 4),
                        ("rus-eng", (5, 10), 3),
                    }
                    else f"score{score}"
                ),
            )
        )
        return OcrEngineOutput(
            text=text,
            words=(
                OcrWord(
                    text,
                    Box(0, 0, min(5, size[0]), 5),
                    type(self).confidence,
                ),
            ),
            geometry=OcrOutputGeometry.WORD_BOXES,
        )

    def close(self) -> None:
        pass


class _IdentityEnhancer:
    def enhance_many(self, crops: tuple[CropInput, ...]) -> tuple[object, ...]:
        return tuple(SimpleNamespace(png_bytes=crop.png_bytes) for crop in crops)


class _RelabelEnhancer:
    def __init__(self) -> None:
        self.calls = 0

    def enhance_many(self, crops: tuple[CropInput, ...]) -> tuple[object, ...]:
        self.calls += 1
        outputs = []
        for crop in crops:
            with Image.open(io.BytesIO(crop.png_bytes)) as opened:
                opened.load()
                label = opened.convert("RGB").getpixel((0, 0))[0]
            outputs.append(SimpleNamespace(png_bytes=_png(label + 1)))
        return tuple(outputs)


def _png(left_label: int, right_label: int | None = None) -> bytes:
    image = Image.new("RGB", (10, 10), (left_label, 255, 255))
    if right_label is not None:
        for x in range(5, 10):
            for y in range(10):
                image.putpixel((x, y), (right_label, 255, 255))
    output = io.BytesIO()
    image.save(output, format="PNG", dpi=(300, 300))
    image.close()
    return output.getvalue()


def _fixtures(
    payloads: tuple[bytes, ...],
) -> tuple[BlockPlan, tuple[BlockCropPair, ...]]:
    source_segment_ids = tuple(f"segment-{index}" for index in range(len(payloads)))
    blocks = tuple(
        RecognitionBlock(
            block_id=f"block-{index:06d}",
            bbox=Box(0, 0, 10, 10),
            core_segment_ids=(f"segment-{index}",),
            segment_ids=source_segment_ids,
            context_segment_ids=tuple(
                segment_id for segment_id in source_segment_ids if segment_id != f"segment-{index}"
            ),
            object_ids=("object-0",),
        )
        for index in range(len(payloads))
    )
    plan = BlockPlan(
        aligned_size=(10, 10),
        source_segment_ids=source_segment_ids,
        blocks=blocks,
        adjacent_algebra=tuple(
            BlockSetAlgebra(
                first_block_id=first.block_id,
                second_block_id=second.block_id,
                intersection_segment_ids=source_segment_ids,
                union_segment_ids=source_segment_ids,
                xor_segment_ids=(),
                first_only_segment_ids=(),
                second_only_segment_ids=(),
            )
            for first, second in zip(blocks, blocks[1:])
        ),
    )
    crops = tuple(
        BlockCropPair(
            block_id=block.block_id,
            bbox=block.bbox,
            segment_ids=block.segment_ids,
            raw=CropInput(f"{block.block_id}-raw", payload),
            gamma=None,
        )
        for block, payload in zip(blocks, payloads)
    )
    return plan, crops


def _session(
    monkeypatch: object,
    scores: dict[tuple[str, tuple[int, int], int], int],
    texts: dict[tuple[str, tuple[int, int], int], str] | None = None,
    *,
    max_workers: int = 1,
) -> AdaptivePersistentOcrSession:
    _Worker.scores = scores
    _Worker.texts = texts or {}
    _Worker.calls = []
    _Worker.call_thread_ids = []
    _Worker.barrier = None
    _Worker.barrier_keys = set()
    _Worker.barrier_passes = 0
    _Worker.score_hook = None
    _Worker.text_hook = None
    _Worker.confidence = 0.99

    def controlled_grammar(
        output: OcrEngineOutput,
        _languages: tuple[str, ...],
    ) -> GrammarAssessment:
        score = int(re.search(r"\d+", output.text).group())
        return GrammarAssessment(score, score >= 97, ())

    monkeypatch.setattr(adaptive, "assess_grammar", controlled_grammar)
    lane = OcrLane(
        lane_id="fake",
        resource=OcrResource.CPU,
        max_workers=max_workers,
        worker_factory=lambda: _Worker(config=_Config(("rus", "eng", "chi_sim"))),
    )
    session = AdaptivePersistentOcrSession((lane,))
    session._enhancer = _IdentityEnhancer()
    return session


def _profile_order(
    block_id: str,
    unit_id: str,
    attempts: tuple[object, ...],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            attempt.profile_id for attempt in attempts if attempt.block_id == block_id and attempt.unit_id == unit_id
        )
    )


def _output(text: str, confidence: float = 0.92) -> OcrEngineOutput:
    words = tuple(
        OcrWord(token, Box(index * 6, 0, index * 6 + 5, 5), confidence) for index, token in enumerate(text.split())
    )
    return OcrEngineOutput(
        text=text,
        words=words,
        geometry=OcrOutputGeometry.WORD_BOXES,
    )


def test_grammar_penalizes_dominant_script_confusables_and_orphan_pipe() -> None:
    noisy = adaptive.assess_grammar(
        _output(
            'Then: "| already have a medical slot, and 5h means losing it" ' "answer: ЭВ",
            0.94,
        ),
        ("rus", "eng"),
    )
    clean = adaptive.assess_grammar(
        _output(
            'Then: "I already have a medical slot, and 5h means losing it" ' "answer: 2h",
            0.92,
        ),
        ("eng",),
    )

    assert noisy.percent < clean.percent
    assert "dominant-script-confusable" in noisy.reasons
    assert "punctuation-confusable" in noisy.reasons
    assert "dominant-script-confusable" not in clean.reasons


def test_grammar_preserves_numeric_separator_evidence() -> None:
    separated = adaptive.assess_grammar(
        _output("You / Me 7 / 8"),
        ("eng",),
    )
    fused = adaptive.assess_grammar(
        _output("You / Me 718"),
        ("eng",),
    )

    assert separated.percent >= fused.percent


def test_latin_dominant_context_selects_english_lock_without_more_calls(
    monkeypatch: object,
) -> None:
    plan, crops = _fixtures((_png(41), _png(42)))
    scores = {
        ("rus-eng", (10, 10), 41): 94,
        ("eng", (10, 10), 41): 92,
        ("eng", (10, 10), 42): 100,
    }
    texts = {
        ("rus-eng", (10, 10), 41): ('score94 Then: "| already have a medical slot" answer: ЭВ'),
        ("eng", (10, 10), 41): ('score92 Then: "I already have a medical slot" answer: 2h'),
        ("eng", (10, 10), 42): "score100 You / Me 7 / 8",
    }
    with _session(monkeypatch, scores, texts) as session:
        result = session.run(plan=plan, crops=crops)

    assert session.state.locked_profile_id == "eng"
    assert session.state.lock_is_provisional is True
    assert tuple(job.lane_id for job in result.jobs) == ("eng", "eng")
    assert _profile_order("block-000000", "full-block", tuple(session.state.attempts)) == (
        "rus-eng",
        "rus",
        "eng",
        "chi_sim",
        "ell",
        "equ",
    )
    assert _profile_order("block-000001", "full-block", tuple(session.state.attempts)) == ("eng",)
    assert len(_Worker.calls) == 8


def test_first_full_block_confirms_rus_eng_lock_at_97(monkeypatch: object) -> None:
    plan, crops = _fixtures((_png(1), _png(2)))
    scores = {
        ("rus-eng", (10, 10), 1): 97,
        ("rus-eng", (10, 10), 2): 100,
    }
    with _session(monkeypatch, scores) as session:
        result = session.run(plan=plan, crops=crops)

        assert session.state.locked_profile_id == "rus-eng"
        assert session.state.lock_is_provisional is False
        assert [call[0] for call in _Worker.calls] == [
            "rus-eng",
            "rus-eng",
        ]
        assert tuple(job.lane_id for job in result.jobs) == (
            "rus-eng",
            "rus-eng",
        )


def test_low_first_block_sweeps_short_profiles_and_sets_provisional_lock(
    monkeypatch: object,
) -> None:
    plan, crops = _fixtures((_png(1), _png(2)))
    scores = {
        ("rus-eng", (10, 10), 1): 50,
        ("rus", (10, 10), 1): 80,
        ("eng", (10, 10), 1): 70,
        ("chi_sim", (10, 10), 1): 95,
        ("ell", (10, 10), 1): 60,
        ("equ", (10, 10), 1): 55,
        ("chi_sim", (10, 10), 2): 100,
    }
    with _session(monkeypatch, scores) as session:
        result = session.run(plan=plan, crops=crops)

        assert session.state.locked_profile_id == "chi_sim"
        assert session.state.lock_is_provisional is True
        assert _profile_order("block-000000", "full-block", tuple(session.state.attempts)) == (
            "rus-eng",
            "rus",
            "eng",
            "chi_sim",
            "ell",
            "equ",
        )
        assert _profile_order("block-000001", "full-block", tuple(session.state.attempts)) == ("chi_sim",)
        assert tuple(job.lane_id for job in result.jobs) == (
            "chi_sim",
            "chi_sim",
        )


def test_low_locked_full_block_uses_chinese_fallback_without_recursion(
    monkeypatch: object,
) -> None:
    plan, crops = _fixtures((_png(1), _png(4)))
    scores = {
        ("rus-eng", (10, 10), 1): 100,
        ("rus-eng", (10, 10), 4): 40,
        ("chi_sim", (10, 10), 4): 100,
    }
    compaction = _two_unit_compaction(plan)
    with _session(monkeypatch, scores) as session:
        result = session.run_with_compaction(
            plan=plan,
            crops=crops,
            compactions=(compaction,),
        )

        assert session.state.locked_profile_id == "rus-eng"
        assert result.jobs[1].lane_id == "chi_sim"
        assert _profile_order("block-000001", "full-block", tuple(session.state.attempts)) == (
            "rus-eng",
            "rus",
            "eng",
            "chi_sim",
        )
        assert all(attempt.unit_id == "full-block" for attempt in session.state.attempts)


def _many_unit_fixture(
    count: int,
) -> tuple[BlockPlan, BlockCropPair, BlockCompaction]:
    columns = 16
    cell_size = 2
    rows = (count + columns - 1) // columns
    image = Image.new(
        "RGB",
        (columns * cell_size, rows * cell_size),
        (255, 255, 255),
    )
    placements = []
    segment_ids = []
    for index in range(count):
        row, column = divmod(index, columns)
        left = column * cell_size
        top = row * cell_size
        bbox = Box(left, top, left + cell_size, top + cell_size)
        label = index % 200 + 1
        for y in range(bbox.top, bbox.bottom):
            for x in range(bbox.left, bbox.right):
                image.putpixel((x, y), (label, 255, 255))
        segment_id = f"segment-{index:04d}"
        segment_ids.append(segment_id)
        placements.append(
            CompactionPlacement(
                unit_id=f"unit-{index:04d}",
                segment_ids=(segment_id,),
                source_bbox=bbox,
                crop_bbox=bbox,
            )
        )
    output = io.BytesIO()
    image.save(output, format="PNG", dpi=(300, 300))
    image.close()
    payload = output.getvalue()
    block = RecognitionBlock(
        block_id="block-000000",
        bbox=Box(0, 0, columns * cell_size, rows * cell_size),
        core_segment_ids=tuple(segment_ids),
        segment_ids=tuple(segment_ids),
        context_segment_ids=(),
        object_ids=("object-0",),
    )
    plan = BlockPlan(
        aligned_size=(columns * cell_size, rows * cell_size),
        source_segment_ids=tuple(segment_ids),
        blocks=(block,),
        adjacent_algebra=(),
    )
    crop = BlockCropPair(
        block_id=block.block_id,
        bbox=block.bbox,
        segment_ids=tuple(segment_ids),
        raw=CropInput(f"{block.block_id}-raw", payload),
        gamma=None,
    )
    compaction = BlockCompaction(
        block_id=block.block_id,
        placements=tuple(placements),
        omitted_empty_units=(),
        occupied_pixels_before=count * cell_size * cell_size,
        occupied_pixels_after=count * cell_size * cell_size,
        packed_canvas_pixels=columns * cell_size * rows * cell_size,
    )
    return plan, crop, compaction


def test_256_clean_units_use_four_context_calls_not_256(
    monkeypatch: object,
) -> None:
    plan, crop, compaction = _many_unit_fixture(256)
    with _session(monkeypatch, {}, max_workers=4) as session:
        session.state.locked_profile_id = "rus-eng"
        _Worker.score_hook = lambda _key, _payload: 100
        candidate = session._recursive_candidate(
            block_id="block-000000",
            block_bbox=plan.blocks[0].bbox,
            crop=crop,
            compaction=compaction,
            fallback_profile_ids=(),
        )

    assert candidate is not None
    assert len(_Worker.calls) == 4
    assert all("context-depth-00" in attempt.unit_id for attempt in session.state.attempts)


def test_256_homogeneous_low_confidence_units_stop_at_16_context(
    monkeypatch: object,
) -> None:
    plan, crop, compaction = _many_unit_fixture(256)
    with _session(monkeypatch, {}, max_workers=4) as session:
        session.state.locked_profile_id = "rus-eng"
        _Worker.score_hook = lambda _key, _payload: 96
        _Worker.confidence = 0.80
        candidate = session._recursive_candidate(
            block_id="block-000000",
            block_bbox=plan.blocks[0].bbox,
            crop=crop,
            compaction=compaction,
            fallback_profile_ids=(),
        )

    assert candidate is not None
    group_ids = {attempt.unit_id for attempt in session.state.attempts}
    assert len(group_ids) <= 4 + 16
    assert sum("context-depth-00" in group_id for group_id in group_ids) == 4
    assert sum("context-depth-01" in group_id for group_id in group_ids) == 16
    assert not any("context-depth-02" in group_id for group_id in group_ids)


def test_feature_only_low_confidence_context_never_reaches_leaf(
    monkeypatch: object,
) -> None:
    plan, crop, compaction = _many_unit_fixture(256)
    with _session(monkeypatch, {}, max_workers=4) as session:
        feature_profile_id = next(
            profile.profile_id
            for profile in session._profiles
            if session._specialized_profile_kind(
                profile.profile_id,
                profile.languages,
            )
            in session._FEATURE_EVIDENCE_KINDS
        )
        assert session._fallback_script_kinds((feature_profile_id,)) == frozenset()
        assert session._fallback_feature_kinds((feature_profile_id,)) == {"math"}
        session.state.locked_profile_id = "rus-eng"
        _Worker.score_hook = lambda _key, _payload: 96
        _Worker.confidence = 0.80
        candidate = session._recursive_candidate(
            block_id="block-000000",
            block_bbox=plan.blocks[0].bbox,
            crop=crop,
            compaction=compaction,
            fallback_profile_ids=(feature_profile_id,),
        )

    assert candidate is not None
    group_ids = {attempt.unit_id for attempt in session.state.attempts}
    assert len(group_ids) <= 4 + 16
    assert not any("context-depth-02" in group_id for group_id in group_ids)
    assert not any("context-depth-03" in group_id for group_id in group_ids)


def test_contextual_composite_and_group_attempt_digests_keep_provenance(
    monkeypatch: object,
    tmp_path,
) -> None:
    plan, crop, compaction = _many_unit_fixture(128)
    with _session(monkeypatch, {}, max_workers=4) as session:
        log_path = tmp_path / "splay.csv"
        session.log_path = log_path
        session.state.enable_attempt_input_artifacts()
        with Image.open(io.BytesIO(crop.raw.png_bytes)) as opened:
            opened.load()
            image = opened.convert("RGB")
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
                    pixels = adaptive.np.asarray(tile_image).copy()
                finally:
                    tile_image.close()
                tiles.append(
                    adaptive._Tile(
                        unit_id=placement.unit_id,
                        segment_ids=placement.segment_ids,
                        source_bbox=placement.source_bbox,
                        pixels=pixels,
                    )
                )
        finally:
            image.close()
        expected_group_digests = {}
        for offset in range(0, len(tiles), 64):
            group = session._build_context_group(
                block_id="block-000000",
                depth=0,
                first_order=offset,
                tiles=tuple(tiles[offset : offset + 64]),
            )
            expected_group_digests[group.group_id] = hashlib.sha256(group.png_bytes).hexdigest()

        session.state.locked_profile_id = "rus-eng"
        _Worker.score_hook = lambda _key, payload: (
            96 if hashlib.sha256(payload).hexdigest() in expected_group_digests.values() else 100
        )
        candidate = session._recursive_candidate(
            block_id="block-000000",
            block_bbox=plan.blocks[0].bbox,
            crop=crop,
            compaction=compaction,
            fallback_profile_ids=(),
        )

    assert candidate is not None
    full_digest = hashlib.sha256(crop.raw.png_bytes).hexdigest()
    assert candidate.input_sha256 == full_digest
    assert candidate.transform is adaptive.OcrTransform.CONTEXTUAL_COMPOSITE
    raw_attempts = tuple(
        attempt for attempt in session.state.attempts if attempt.transform is adaptive.OcrTransform.RAW
    )
    assert {attempt.unit_id: attempt.input_sha256 for attempt in raw_attempts} == expected_group_digests
    assert full_digest not in expected_group_digests.values()
    gamma_attempts = tuple(
        attempt for attempt in session.state.attempts if attempt.transform is adaptive.OcrTransform.GAMMA
    )
    assert gamma_attempts
    raw_by_unit = {attempt.unit_id: attempt.input_sha256 for attempt in raw_attempts}
    attempts_csv = (tmp_path / "splay-attempts.csv").read_text(encoding="utf-8")
    for attempt in gamma_attempts:
        assert attempt.input_sha256 != raw_by_unit[attempt.unit_id]
        gamma_artifact = tmp_path / "splay-attempt-inputs" / f"{attempt.input_sha256}.png"
        gamma_payload = gamma_artifact.read_bytes()
        assert hashlib.sha256(gamma_payload).hexdigest() == (attempt.input_sha256)
        assert f"splay-attempt-inputs/{attempt.input_sha256}.png" in attempts_csv


def test_only_one_bad_64_group_descends_to_its_four_children(
    monkeypatch: object,
) -> None:
    plan, crop, compaction = _many_unit_fixture(128)
    gate_calls = 0

    def selective_gate(
        _candidate: object,
        _fallback_profile_ids: tuple[str, ...],
        *,
        missing_native_script: bool | None = None,
    ) -> bool:
        del missing_native_script
        nonlocal gate_calls
        gate_calls += 1
        return gate_calls == 1

    with _session(monkeypatch, {}, max_workers=4) as session:
        session.state.locked_profile_id = "rus-eng"
        _Worker.score_hook = lambda _key, _payload: 100
        monkeypatch.setattr(
            session,
            "_candidate_requires_context_recursion",
            selective_gate,
        )
        candidate = session._recursive_candidate(
            block_id="block-000000",
            block_bbox=plan.blocks[0].bbox,
            crop=crop,
            compaction=compaction,
            fallback_profile_ids=(),
        )

    assert candidate is not None
    assert len(_Worker.calls) == 6
    unit_ids = tuple(dict.fromkeys(attempt.unit_id for attempt in session.state.attempts))
    assert sum("context-depth-00" in unit_id for unit_id in unit_ids) == 2
    assert sum("context-depth-01" in unit_id for unit_id in unit_ids) == 4


def test_missing_cjk_context_descends_to_leaf_units(
    monkeypatch: object,
) -> None:
    plan, crop, compaction = _many_unit_fixture(4)

    with _session(monkeypatch, {}, max_workers=4) as session:
        primary_profile_id = session._DEFAULT_PROFILE_ID
        cjk_profile_id = next(
            profile.profile_id
            for profile in session._profiles
            if session._specialized_profile_kind(
                profile.profile_id,
                profile.languages,
            )
            == "cjk"
        )

        def score_hook(
            key: tuple[str, tuple[int, int], int],
            _payload: bytes,
        ) -> int:
            return 96 if key[0] == primary_profile_id else 50

        def text_hook(
            key: tuple[str, tuple[int, int], int],
            _payload: bytes,
        ) -> str:
            return "score96" if key[0] == primary_profile_id else "中文score50"

        session.state.locked_profile_id = primary_profile_id
        _Worker.score_hook = score_hook
        _Worker.text_hook = text_hook
        candidate = session._recursive_candidate(
            block_id="block-000000",
            block_bbox=plan.blocks[0].bbox,
            crop=crop,
            compaction=compaction,
            fallback_profile_ids=(cjk_profile_id,),
        )

    assert candidate is not None
    unit_ids = {attempt.unit_id for attempt in session.state.attempts}
    assert any("context-depth-01" in unit_id and "units-001" in unit_id for unit_id in unit_ids)
    assert not any("context-depth-02" in unit_id for unit_id in unit_ids)


def test_only_mixed_script_branch_descends_below_16_context(
    monkeypatch: object,
) -> None:
    plan, crop, compaction = _many_unit_fixture(64)
    missing_calls = 0

    def selective_missing(
        _candidate: object,
        _fallback_profile_ids: tuple[str, ...],
    ) -> bool:
        nonlocal missing_calls
        missing_calls += 1
        return missing_calls in {1, 2, 6}

    with _session(monkeypatch, {}, max_workers=4) as session:
        session.state.locked_profile_id = "rus-eng"
        _Worker.score_hook = lambda _key, _payload: 96
        _Worker.text_hook = lambda key, _payload: ("中文96" if key[0] == "chi_sim" else "score96")
        monkeypatch.setattr(
            session,
            "_missing_evidenced_native_script",
            selective_missing,
        )
        candidate = session._recursive_candidate(
            block_id="block-000000",
            block_bbox=plan.blocks[0].bbox,
            crop=crop,
            compaction=compaction,
            fallback_profile_ids=("chi_sim",),
        )

    assert candidate is not None
    group_ids = {attempt.unit_id for attempt in session.state.attempts}
    assert sum("context-depth-00" in group_id for group_id in group_ids) == 1
    assert sum("context-depth-01" in group_id for group_id in group_ids) == 4
    assert sum("context-depth-02" in group_id for group_id in group_ids) == 4
    assert sum("context-depth-03" in group_id for group_id in group_ids) == 4
    assert all(
        "order-000000" in group_id or "context-depth-00" in group_id or "context-depth-01" in group_id
        for group_id in group_ids
        if "context-depth-02" in group_id
    )


def test_missing_native_script_selects_specialized_profile_only_at_leaf(
    monkeypatch: object,
) -> None:
    plan, crop, compaction = _many_unit_fixture(4)

    with _session(monkeypatch, {}, max_workers=1) as session:
        session.state.locked_profile_id = "rus-eng"
        _Worker.score_hook = lambda _key, _payload: 96
        _Worker.text_hook = lambda key, _payload: ("中文50" if key[0] == "chi_sim" else "score96")
        _Worker.confidence = 0.70
        candidate = session._recursive_candidate(
            block_id="block-000000",
            block_bbox=plan.blocks[0].bbox,
            crop=crop,
            compaction=compaction,
            fallback_profile_ids=("chi_sim",),
        )

    assert candidate is not None
    assert len(candidate.output.words) == 4
    assert all("中文" in word.text for word in candidate.output.words)
    full_candidate = replace(
        candidate,
        profile=next(profile for profile in session._profiles if profile.profile_id == "rus-eng"),
        transform=adaptive.OcrTransform.RAW,
        output=_output("score96"),
        assessment=GrammarAssessment(96, False, ()),
    )
    patched = session._patch_missing_native_units(
        full_candidate,
        candidate,
        compaction,
        ("chi_sim",),
    )
    assert patched is not None
    assert "中文" in patched.output.text
    assert "score96" not in patched.output.text
    assert not any(
        attempt.profile_id == "chi_sim" and "context-depth-00" in attempt.unit_id and attempt.grammar_percent >= 97
        for attempt in session.state.attempts
    )


def test_canonical_full_output_normalizes_to_bound_crop(
    monkeypatch: object,
) -> None:
    plan, _crop, compaction = _many_unit_fixture(4)
    output = OcrEngineOutput(
        text="score97",
        words=(OcrWord("score97", Box(1, 1, 3, 2), 0.99),),
        geometry=OcrOutputGeometry.WORD_BOXES,
    )

    with _session(monkeypatch, {}, max_workers=1) as session:
        canonical = replace(
            compaction,
            raster_kind=adaptive.CompactionRasterKind.CANONICAL_LOCALITY,
        )
        slots = {placement.unit_id: index for index, placement in enumerate(canonical.placements)}
        canonical_output = session._map_full_output(
            output,
            canonical,
            Box(20, 20, 22, 22),
            slots,
            (2, 2),
        )
        spatial_output = session._map_full_output(
            output,
            compaction,
            Box(20, 20, 22, 22),
            slots,
            (2, 2),
        )

    placement = max(
        canonical.placements,
        key=lambda item: output.words[0].bbox.intersection_area(item.crop_bbox),
    )
    slot = slots[placement.unit_id]
    assert canonical_output.text == output.text
    assert canonical_output.words == (
        OcrWord(
            "score97",
            Box(slot % 2, slot // 2, slot % 2 + 1, slot // 2 + 1),
            0.99,
        ),
    )
    assert spatial_output != output


def test_canonical_membership_bound_uses_crop_not_logical_window(
    monkeypatch: object,
) -> None:
    plan, crop, compaction = _many_unit_fixture(4)
    crop = replace(
        crop,
        bbox=Box(0, 0, 438, 101),
        raw=CropInput(
            f"{crop.block_id}-raw",
            adaptive._png_bytes(np.full((101, 438, 3), 255, dtype=np.uint8)),
        ),
    )
    canonical_block = replace(
        plan.blocks[0],
        bbox=Box(0, 0, 3, 1),
    )
    plan = replace(
        plan,
        aligned_size=(3, 2),
        blocks=(canonical_block,),
    )
    canonical = replace(
        compaction,
        raster_kind=adaptive.CompactionRasterKind.CANONICAL_LOCALITY,
    )
    output = OcrEngineOutput(
        text="score97",
        words=(OcrWord("score97", Box(0, 0, 2, 2), 0.99),),
        geometry=OcrOutputGeometry.WORD_BOXES,
    )

    with _session(monkeypatch, {}, max_workers=1) as session:
        contracts = session._canonical_membership_contract(
            plan,
            (crop,),
            (canonical,),
        )
        slots, slot_size = contracts[canonical_block.block_id]
        b201_slots = dict(slots)
        for slot in range(len(b201_slots), 2945):
            b201_slots[f"b201-membership-{slot:06d}"] = slot
        mapped = session._map_full_output(
            output,
            canonical,
            canonical_block.bbox,
            b201_slots,
            slot_size,
        )
        overflow_slots = {f"overflow-membership-{slot:06d}": slot for slot in range(slot_size[0] * slot_size[1] + 1)}
        try:
            session._map_full_output(
                output,
                canonical,
                canonical_block.bbox,
                overflow_slots,
                slot_size,
            )
        except ValueError as error:
            assert str(error) == ("canonical locality membership slots exceed bound crop")
        else:
            raise AssertionError("canonical crop overflow must fail closed")

    assert slot_size == (
        crop.bbox.width,
        crop.bbox.height,
    )
    assert len(b201_slots) == 2945
    assert mapped.words


def test_doc_course_canonical_slots_use_forward_crop_canvas(
    monkeypatch: object,
) -> None:
    plan, crop, compaction = _many_unit_fixture(87)
    crop = replace(
        crop,
        bbox=Box(0, 0, 720, 1406),
        raw=CropInput(
            f"{crop.block_id}-raw",
            adaptive._png_bytes(np.full((1406, 720, 3), 255, dtype=np.uint8)),
        ),
    )
    block = replace(plan.blocks[0], bbox=Box(0, 0, 59, 13))
    plan = replace(plan, aligned_size=(59, 13), blocks=(block,))
    canonical = replace(
        compaction,
        raster_kind=adaptive.CompactionRasterKind.CANONICAL_LOCALITY,
    )
    output = OcrEngineOutput(
        text=" ".join(f"unit-{index}" for index in range(87)),
        words=tuple(
            OcrWord(
                f"unit-{index}",
                placement.crop_bbox,
                0.99,
            )
            for index, placement in enumerate(canonical.placements)
        ),
        geometry=OcrOutputGeometry.WORD_BOXES,
    )

    with _session(monkeypatch, {}, max_workers=1) as session:
        contracts = session._canonical_membership_contract(
            plan,
            (crop,),
            (canonical,),
        )
        slots, slot_size = contracts[block.block_id]
        mapped = session._map_full_output(
            output,
            canonical,
            block.bbox,
            slots,
            slot_size,
        )

    assert slot_size == (720, 1406)
    assert max(word.bbox.right for word in mapped.words) == 87
    assert max(word.bbox.right for word in mapped.words) > block.bbox.width
    assert all(word.bbox.intersection(Box(0, 0, *slot_size)) == word.bbox for word in mapped.words)


def test_ucheb_small_component_does_not_inherit_document_slot_namespace(
    monkeypatch: object,
) -> None:
    segment_ids = tuple(f"small-segment-{index:02d}" for index in range(25))
    placements = tuple(
        MatrixLocalPlacement(
            segment_id=segment_id,
            local_region_id="local-region-000000",
            island_id="island-000000",
            polar_order=index,
            region_order=index,
            table_row=index // 16,
            table_column=index % 16,
            matrix_row=index // 16,
            matrix_column=index % 16,
        )
        for index, segment_id in enumerate(segment_ids)
    )

    def local_block(
        block_id: str,
        selected: tuple[int, ...],
        kind: str,
    ) -> RecognitionBlock:
        selected_ids = tuple(segment_ids[index] for index in selected)
        return RecognitionBlock(
            block_id=block_id,
            bbox=Box(0, 0, 320, 40),
            core_segment_ids=selected_ids,
            segment_ids=selected_ids,
            context_segment_ids=(),
            object_ids=("object-000024",),
            matrix_window=(0, 12, 0, 4),
            matrix_window_kind=kind,
            matrix_segment_shape=(2, max(1, len(selected_ids))),
            local_islands=(
                MatrixLocalIsland(
                    local_region_id="local-region-000000",
                    island_id="island-000000",
                    segment_ids=selected_ids,
                    region_boundary=(0, 25),
                    matrix_segment_shape=(2, 16),
                ),
            ),
            local_placements=tuple(placement for placement in placements if placement.segment_id in selected_ids),
        )

    full_block = local_block(
        "block-small-0",
        tuple(range(25)),
        "polar-local-full",
    )
    tiles = tuple(
        adaptive._Tile(
            unit_id=f"small-unit-{index:02d}",
            segment_ids=(segment_id,),
            source_bbox=Box(
                (index % 16) * 12,
                (index // 16) * 10,
                (index % 16) * 12 + 10,
                (index // 16) * 10 + 8,
            ),
            pixels=np.full((8, 10, 3), 32 + index, dtype=np.uint8),
        )
        for index, segment_id in enumerate(segment_ids)
    )
    render_slots = {tile.unit_id: index for index, tile in enumerate(tiles)}
    render_slot_shape = (2, 13)
    layouts = adaptive._derive_local_region_layouts(
        tiles,
        full_block,
        membership_slot_by_id=render_slots,
        slot_shape=render_slot_shape,
    )
    rendered = adaptive._render_canonical_locality_raster(
        tiles,
        full_block,
        region_layouts=layouts,
        membership_slot_by_id=render_slots,
        slot_shape=render_slot_shape,
    )
    signature_indexes = tuple(index for index in range(25) if index % 5 == 0)
    signature_block = local_block(
        "block-small-1",
        signature_indexes,
        "polar-local-signature",
    )
    signature_tiles = tuple(tiles[index] for index in signature_indexes)
    signature_rendered = adaptive._render_canonical_locality_raster(
        signature_tiles,
        signature_block,
        region_layouts=layouts,
        membership_slot_by_id=render_slots,
        slot_shape=render_slot_shape,
    )

    assert layouts["local-region-000000"].matrix_segment_shape == (13, 2)
    assert rendered.size[0] < 1_000
    assert rendered.size[1] < 1_000
    assert signature_rendered.size == rendered.size
    full_bbox_by_unit = {placement.unit_id: placement.crop_bbox for placement in rendered.placements}
    assert all(
        placement.crop_bbox == full_bbox_by_unit[placement.unit_id] for placement in signature_rendered.placements
    )

    small_block_ids = tuple(f"block-small-{index}" for index in range(6))
    large_block_id = "block-large-4516"
    small_units = tuple(
        SimpleNamespace(
            unit_id=f"small-unit-{index:02d}",
            block_ids=(small_block_ids[0], small_block_ids[1 + index % 5]),
        )
        for index in range(25)
    )
    large_units = tuple(
        SimpleNamespace(
            unit_id=f"large-unit-{index:04d}",
            block_ids=(large_block_id,),
        )
        for index in range(4516)
    )
    plan = SimpleNamespace(
        blocks=tuple(SimpleNamespace(block_id=block_id) for block_id in (large_block_id, *small_block_ids)),
        membership_units=large_units + small_units,
    )
    membership_by_unit = {unit.unit_id: unit.block_ids for unit in small_units}
    small_placements_by_block = {
        block_id: tuple(
            placement for placement in rendered.placements if block_id in membership_by_unit[placement.unit_id]
        )
        for block_id in small_block_ids
    }
    crops = (
        SimpleNamespace(block_id=large_block_id, bbox=Box(0, 0, 19076, 76)),
        *(
            SimpleNamespace(
                block_id=block_id,
                bbox=Box(0, 0, *rendered.size),
            )
            for block_id in small_block_ids
        ),
    )
    compactions = (
        SimpleNamespace(
            block_id=large_block_id,
            placements=(),
            raster_kind=adaptive.CompactionRasterKind.CANONICAL_LOCALITY,
        ),
        *(
            SimpleNamespace(
                block_id=block_id,
                placements=small_placements_by_block[block_id],
                raster_kind=adaptive.CompactionRasterKind.CANONICAL_LOCALITY,
            )
            for block_id in small_block_ids
        ),
    )

    with _session(monkeypatch, {}, max_workers=1) as session:
        contracts = session._canonical_membership_contract(
            plan,
            crops,
            compactions,
        )
        small_maps = tuple(contracts[block_id][0] for block_id in small_block_ids)
        selected_compaction = compactions[2]
        words = tuple(
            OcrWord(
                placement.unit_id,
                placement.crop_bbox,
                0.99,
            )
            for placement in selected_compaction.placements
        )
        mapped = session._map_full_output(
            OcrEngineOutput(
                text=" ".join(word.text for word in words),
                words=words,
                geometry=OcrOutputGeometry.WORD_BOXES,
            ),
            selected_compaction,
            Box(0, 0, *rendered.size),
            small_maps[1],
            contracts[small_block_ids[1]][1],
        )

    assert len(contracts[large_block_id][0]) == 4516
    assert all(slot_map == small_maps[0] for slot_map in small_maps)
    assert len(small_maps[0]) == 25
    first_members = set(small_placements_by_block[small_block_ids[0]])
    second_members = set(small_placements_by_block[small_block_ids[1]])
    assert first_members & second_members
    assert first_members ^ second_members
    assert len(mapped.words) == len(selected_compaction.placements)
    assert all(
        word.bbox
        == Box(
            small_maps[1][word.text] % rendered.size[0],
            small_maps[1][word.text] // rendered.size[0],
            small_maps[1][word.text] % rendered.size[0] + 1,
            small_maps[1][word.text] // rendered.size[0] + 1,
        )
        for word in mapped.words
    )


def test_hierarchical_context_order_is_max_worker_independent(
    monkeypatch: object,
) -> None:
    plan, crop, compaction = _many_unit_fixture(130)

    def execute(max_workers: int) -> tuple[object, tuple[object, ...]]:
        with _session(monkeypatch, {}, max_workers=max_workers) as session:
            session.state.locked_profile_id = "rus-eng"
            _Worker.score_hook = lambda _key, _payload: 100
            _Worker.text_hook = lambda key, _payload: f"score100-label-{key[2]}"
            candidate = session._recursive_candidate(
                block_id="block-000000",
                block_bbox=plan.blocks[0].bbox,
                crop=crop,
                compaction=compaction,
                fallback_profile_ids=(),
            )
        assert candidate is not None
        attempts = tuple(
            (
                attempt.unit_id,
                attempt.profile_id,
                attempt.transform,
                attempt.text,
            )
            for attempt in session.state.attempts
        )
        return candidate.output, attempts

    assert execute(4) == execute(1)


def test_hierarchical_mapped_words_stay_inside_source_placements(
    monkeypatch: object,
) -> None:
    plan, crop, compaction = _many_unit_fixture(64)
    with _session(monkeypatch, {}, max_workers=4) as session:
        session.state.locked_profile_id = "rus-eng"
        _Worker.score_hook = lambda _key, _payload: 100
        candidate = session._recursive_candidate(
            block_id="block-000000",
            block_bbox=plan.blocks[0].bbox,
            crop=crop,
            compaction=compaction,
            fallback_profile_ids=(),
        )

    assert candidate is not None
    local_sources = tuple(
        Box(
            placement.source_bbox.left - plan.blocks[0].bbox.left,
            placement.source_bbox.top - plan.blocks[0].bbox.top,
            placement.source_bbox.right - plan.blocks[0].bbox.left,
            placement.source_bbox.bottom - plan.blocks[0].bbox.top,
        )
        for placement in compaction.placements
    )
    assert candidate.output.words
    assert all(
        any(
            source.left <= word.bbox.left
            and source.top <= word.bbox.top
            and source.right >= word.bbox.right
            and source.bottom >= word.bbox.bottom
            for source in local_sources
        )
        for word in candidate.output.words
    )


def _multi_block_recursion_fixture(
    count: int,
) -> tuple[BlockPlan, tuple[BlockCropPair, ...], tuple[BlockCompaction, ...]]:
    plan, crops = _fixtures(tuple(_png(40 + index * 2, 41 + index * 2) for index in range(count)))
    compactions = tuple(_two_unit_compaction(plan, block_index) for block_index, _block in enumerate(plan.blocks))
    return plan, crops, compactions


def test_recursion_blocks_overlap_without_exceeding_max_workers(
    monkeypatch: object,
) -> None:
    plan, crops, compactions = _multi_block_recursion_fixture(4)
    original = adaptive.AdaptivePersistentOcrSession._recursive_candidate
    barrier = threading.Barrier(2)
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def synchronized_recursive(
        child: AdaptivePersistentOcrSession,
        **kwargs: object,
    ) -> object:
        nonlocal active, maximum_active
        assert child._max_workers == 1
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            barrier.wait(timeout=2.0)
            return original(child, **kwargs)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(
        adaptive.AdaptivePersistentOcrSession,
        "_recursive_candidate",
        synchronized_recursive,
    )
    with _session(monkeypatch, {}, max_workers=2) as session:
        _Worker.score_hook = lambda _key, _payload: 96
        _Worker.confidence = 0.80
        result = session.run_with_compaction(
            plan=plan,
            crops=crops,
            compactions=compactions,
        )

    assert result.complete == 4
    assert maximum_active == 2
    assert maximum_active <= session._max_workers


def test_parallel_recursion_blocks_preserve_output_attempt_and_splay_order(
    monkeypatch: object,
) -> None:
    plan, crops, compactions = _multi_block_recursion_fixture(3)

    def execute(max_workers: int) -> tuple[object, object, object]:
        with _session(monkeypatch, {}, max_workers=max_workers) as session:
            _Worker.score_hook = lambda _key, _payload: 96
            _Worker.text_hook = lambda key, _payload: f"score96-label-{key[2]}"
            _Worker.confidence = 0.80
            result = session.run_with_compaction(
                plan=plan,
                crops=crops,
                compactions=compactions,
            )
        jobs = tuple(
            (
                job.block_id,
                job.lane_id,
                job.output,
            )
            for job in result.jobs
        )
        attempts = tuple(
            (
                attempt.block_id,
                attempt.unit_id,
                attempt.profile_id,
                attempt.transform,
                attempt.text,
            )
            for attempt in session.state.attempts
        )
        splay = tuple(
            (
                node.profile_id,
                node.attempts,
                node.exact_grammar,
                node.garbage,
                node.last_grammar_percent,
                node.quality_sum,
            )
            for node in session.state.nodes
        )
        recursive_block_order = tuple(
            attempt.block_id for attempt in session.state.attempts if ":context-depth-" in attempt.unit_id
        )
        expected_order = {block.block_id: index for index, block in enumerate(plan.blocks)}
        assert tuple(expected_order[block_id] for block_id in recursive_block_order) == tuple(
            sorted(expected_order[block_id] for block_id in recursive_block_order)
        )
        return jobs, attempts, splay

    assert execute(3) == execute(1)


def test_context_mapping_uses_original_compact_placement_for_polar_blocks(
    monkeypatch: object,
) -> None:
    group_placements = (
        CompactionPlacement(
            unit_id="first",
            segment_ids=("first",),
            source_bbox=Box(100, 100, 120, 120),
            crop_bbox=Box(0, 0, 20, 20),
        ),
        CompactionPlacement(
            unit_id="later-polar",
            segment_ids=("later",),
            source_bbox=Box(10, 10, 20, 20),
            crop_bbox=Box(28, 0, 48, 20),
        ),
    )
    original_placements = {
        "first": CompactionPlacement(
            unit_id="first",
            segment_ids=("first",),
            source_bbox=Box(100, 100, 120, 120),
            crop_bbox=Box(0, 0, 20, 20),
        ),
        "later-polar": CompactionPlacement(
            unit_id="later-polar",
            segment_ids=("later",),
            source_bbox=Box(10, 10, 20, 20),
            crop_bbox=Box(40, 30, 50, 40),
        ),
    }
    output = OcrEngineOutput(
        text="later",
        words=(OcrWord("later", Box(30, 4, 40, 14), 0.90),),
        geometry=OcrOutputGeometry.WORD_BOXES,
    )
    with _session(monkeypatch, {}) as session:
        mapped = session._map_context_output(
            output,
            group_placements,
            original_placements,
            Box(0, 0, 60, 50),
        )

    assert mapped.words[0].bbox == Box(41, 32, 46, 37)
    assert original_placements["later-polar"].crop_bbox.left <= (mapped.words[0].bbox.left)
    assert original_placements["later-polar"].crop_bbox.top <= (mapped.words[0].bbox.top)


def test_context_mapping_keeps_ordinary_spatial_compact_coordinates(
    monkeypatch: object,
) -> None:
    placement = CompactionPlacement(
        unit_id="ordinary",
        segment_ids=("ordinary",),
        source_bbox=Box(10, 5, 30, 15),
        crop_bbox=Box(10, 5, 30, 15),
    )
    output = OcrEngineOutput(
        text="ordinary",
        words=(OcrWord("ordinary", Box(12, 6, 20, 10), 0.90),),
        geometry=OcrOutputGeometry.WORD_BOXES,
    )
    with _session(monkeypatch, {}) as session:
        mapped = session._map_context_output(
            output,
            (placement,),
            {"ordinary": placement},
            Box(0, 0, 40, 20),
        )

    assert mapped == output


def test_fallback_filter_distinguishes_none_shortlist_and_empty_tuple(
    monkeypatch: object,
) -> None:
    scores = {
        ("rus-eng", (10, 10), 11): 40,
        ("rus", (10, 10), 11): 50,
        ("eng", (10, 10), 11): 60,
        ("chi_sim", (10, 10), 11): 80,
        ("ell", (10, 10), 11): 30,
        ("equ", (10, 10), 11): 20,
        ("rus-eng", (10, 10), 12): 40,
        ("chi_sim", (10, 10), 12): 80,
        ("rus-eng", (10, 10), 13): 40,
        ("chi_sim", (10, 10), 13): 80,
    }
    with _session(monkeypatch, scores) as session:
        session._recognize_image(
            block_id="all",
            unit_id="unit",
            raw_png=_png(11),
            primary_profile_id="rus-eng",
            transforms=(adaptive.OcrTransform.RAW,),
            fallback_profile_ids=None,
        )
        session._recognize_image(
            block_id="shortlist",
            unit_id="unit",
            raw_png=_png(12),
            primary_profile_id="rus-eng",
            transforms=(adaptive.OcrTransform.RAW,),
            fallback_profile_ids=("chi_sim",),
        )
        session._recognize_image(
            block_id="primary-only",
            unit_id="unit",
            raw_png=_png(13),
            primary_profile_id="rus-eng",
            transforms=(adaptive.OcrTransform.RAW,),
            fallback_profile_ids=(),
        )

    attempts = tuple(session.state.attempts)
    assert _profile_order("all", "unit", attempts) == (
        "rus-eng",
        "rus",
        "eng",
        "chi_sim",
        "ell",
        "equ",
    )
    assert _profile_order("shortlist", "unit", attempts) == (
        "rus-eng",
        "chi_sim",
    )
    assert _profile_order("primary-only", "unit", attempts) == ("rus-eng",)


def test_raw_at_target_skips_gamma_and_keeps_preprocess_lazy(
    monkeypatch: object,
) -> None:
    scores = {
        ("rus-eng", (10, 10), 20): 97,
        ("rus-eng", (10, 10), 21): 100,
    }
    with _session(monkeypatch, scores) as session:
        enhancer = _RelabelEnhancer()
        session._enhancer = enhancer
        decision = session._recognize_image(
            block_id="raw-target",
            unit_id="unit",
            raw_png=_png(20),
            primary_profile_id="rus-eng",
            transforms=(adaptive.OcrTransform.RAW, adaptive.OcrTransform.GAMMA),
            fallback_profile_ids=(),
        )

    assert decision.candidate is not None
    assert decision.candidate.transform is adaptive.OcrTransform.RAW
    assert enhancer.calls == 0
    assert tuple(attempt.transform for attempt in session.state.attempts) == (adaptive.OcrTransform.RAW,)


def test_raw_below_target_runs_gamma(
    monkeypatch: object,
) -> None:
    scores = {
        ("rus-eng", (10, 10), 22): 96,
        ("rus-eng", (10, 10), 23): 98,
    }
    with _session(monkeypatch, scores) as session:
        enhancer = _RelabelEnhancer()
        session._enhancer = enhancer
        decision = session._recognize_image(
            block_id="raw-low",
            unit_id="unit",
            raw_png=_png(22),
            primary_profile_id="rus-eng",
            transforms=(adaptive.OcrTransform.RAW, adaptive.OcrTransform.GAMMA),
            fallback_profile_ids=(),
        )

    assert decision.candidate is not None
    assert decision.candidate.transform is adaptive.OcrTransform.GAMMA
    assert enhancer.calls == 1
    assert tuple(attempt.transform for attempt in session.state.attempts) == (
        adaptive.OcrTransform.RAW,
        adaptive.OcrTransform.GAMMA,
    )


def test_gamma_does_not_replace_a_better_raw_candidate(
    monkeypatch: object,
) -> None:
    scores = {
        ("rus-eng", (10, 10), 24): 96,
        ("rus-eng", (10, 10), 25): 90,
    }
    with _session(monkeypatch, scores) as session:
        enhancer = _RelabelEnhancer()
        session._enhancer = enhancer
        decision = session._recognize_image(
            block_id="raw-best",
            unit_id="unit",
            raw_png=_png(24),
            primary_profile_id="rus-eng",
            transforms=(adaptive.OcrTransform.RAW, adaptive.OcrTransform.GAMMA),
            fallback_profile_ids=(),
        )

    assert decision.candidate is not None
    assert decision.candidate.transform is adaptive.OcrTransform.RAW
    assert decision.candidate.assessment.percent == 96
    assert enhancer.calls == 1


def _language_attempt(
    sequence: int,
    profile_id: str,
    languages: tuple[str, ...],
    text: str,
    mean_confidence: float,
) -> adaptive.LanguageAttempt:
    return adaptive.LanguageAttempt(
        sequence=sequence,
        block_id="block",
        unit_id="full-block",
        profile_id=profile_id,
        languages=languages,
        transform=adaptive.OcrTransform.RAW,
        input_sha256=f"{sequence:064x}",
        status="complete",
        grammar_percent=50,
        mean_confidence=mean_confidence,
        text=text,
        error="",
        elapsed_seconds=0.0,
    )


def test_specialized_evidence_is_confident_dense_and_splay_ordered(
    monkeypatch: object,
) -> None:
    with _session(monkeypatch, {}) as session:
        monkeypatch.setattr(
            session.state,
            "ordered_profile_ids",
            lambda: ("equ", "chi_sim", "ell", "rus-eng", "rus", "eng"),
        )
        fallback = session._specialized_fallback_profile_ids(
            (
                _language_attempt(
                    1,
                    "chi_sim",
                    ("chi_sim",),
                    "中文样本 score80",
                    0.80,
                ),
                _language_attempt(
                    2,
                    "rus-eng",
                    ("rus", "eng"),
                    "α + β score70",
                    0.50,
                ),
                _language_attempt(
                    3,
                    "eng",
                    ("eng",),
                    "x = 2 score70",
                    0.50,
                ),
            )
        )

    assert fallback == ("equ", "chi_sim", "ell")


def test_specialized_own_script_hallucinations_do_not_create_evidence(
    monkeypatch: object,
) -> None:
    with _session(monkeypatch, {}) as session:
        fallback = session._specialized_fallback_profile_ids(
            (
                _language_attempt(
                    1,
                    "chi_sim",
                    ("chi_sim",),
                    "中文样本",
                    0.56,
                ),
                _language_attempt(
                    2,
                    "chi_sim",
                    ("chi_sim",),
                    "中文 " + "a" * 300,
                    0.80,
                ),
                _language_attempt(
                    3,
                    "ell",
                    ("ell",),
                    "α β γ",
                    0.90,
                ),
                _language_attempt(
                    4,
                    "equ",
                    ("equ",),
                    "x = 2",
                    0.90,
                ),
            )
        )

    assert fallback == ()


def test_native_run_reconciliation_is_monotonic_and_skips_hallucinations(
    monkeypatch: object,
) -> None:
    recursive = OcrEngineOutput(
        text="start 测试效气 separator 数字九 end",
        words=(
            OcrWord("start", Box(0, 0, 5, 5), 0.90),
            OcrWord("测试效气", Box(10, 0, 30, 10), 0.80),
            OcrWord("separator", Box(35, 0, 45, 5), 0.90),
            OcrWord("数字九", Box(50, 0, 65, 10), 0.85),
            OcrWord("end", Box(70, 0, 75, 5), 0.90),
        ),
        geometry=OcrOutputGeometry.WORD_BOXES,
    )
    specialized = (
        adaptive._NativeScriptRun("cjk", "部分甲", 0.99),
        adaptive._NativeScriptRun("cjk", "测试数据", 0.95),
        adaptive._NativeScriptRun("cjk", "乙丙", 0.99),
        adaptive._NativeScriptRun("cjk", "数字九", 0.96),
    )
    with _session(monkeypatch, {}) as session:
        reconciled = session._reconcile_native_runs(recursive, specialized)

    assert tuple(word.text for word in reconciled.words) == (
        "start",
        "测试数据",
        "separator",
        "数字九",
        "end",
    )


def test_native_run_reconciliation_rejects_unmatched_and_low_confidence(
    monkeypatch: object,
) -> None:
    recursive = OcrEngineOutput(
        text="before 测试效气 after",
        words=(
            OcrWord("before", Box(0, 0, 5, 5), 0.90),
            OcrWord("测试效气", Box(10, 0, 30, 10), 0.90),
            OcrWord("after", Box(35, 0, 40, 5), 0.90),
        ),
        geometry=OcrOutputGeometry.WORD_BOXES,
    )
    specialized = (
        adaptive._NativeScriptRun("cjk", "部分甲", 0.99),
        adaptive._NativeScriptRun("cjk", "测试数据", 0.89),
        adaptive._NativeScriptRun("cjk", "乙丙", 0.99),
    )
    with _session(monkeypatch, {}) as session:
        reconciled = session._reconcile_native_runs(recursive, specialized)

    assert reconciled is recursive
    assert tuple(word.text for word in reconciled.words) == (
        "before",
        "测试效气",
        "after",
    )


def test_native_run_reconciliation_preserves_order_and_uses_recursive_bbox_union(
    monkeypatch: object,
) -> None:
    before = OcrWord("before", Box(0, 0, 5, 5), 0.90)
    after = OcrWord("after", Box(40, 0, 45, 5), 0.90)
    recursive = OcrEngineOutput(
        text="before 测试 效气 after",
        words=(
            before,
            OcrWord("测试", Box(10, 5, 20, 15), 0.80),
            OcrWord("效气", Box(20, 5, 30, 15), 0.80),
            after,
        ),
        geometry=OcrOutputGeometry.WORD_BOXES,
    )
    with _session(monkeypatch, {}) as session:
        reconciled = session._reconcile_native_runs(
            recursive,
            (adaptive._NativeScriptRun("cjk", "测试数据", 0.95),),
        )

    assert tuple(word.text for word in reconciled.words) == (
        "before",
        "测试数据",
        "after",
    )
    assert reconciled.words[0] == before
    assert reconciled.words[1].bbox == Box(10, 5, 30, 15)
    assert reconciled.words[1].confidence == 0.95
    assert reconciled.words[2] == after
    assert reconciled.text == "before 测试数据 after"


def _full_block_recognition(
    session: AdaptivePersistentOcrSession,
    *,
    text: str,
    grammar_percent: int,
    confidence: float,
    fallback_profile_ids: tuple[str, ...] = (),
) -> adaptive._FullBlockRecognition:
    profile = next(profile for profile in session._profiles if profile.profile_id == "rus-eng")
    output = _output(text, confidence)
    return adaptive._FullBlockRecognition(
        candidate=adaptive._Candidate(
            profile=profile,
            transform=adaptive.OcrTransform.RAW,
            output=output,
            assessment=GrammarAssessment(
                grammar_percent,
                grammar_percent == 100,
                (),
            ),
            input_sha256="c" * 64,
            elapsed_seconds=0.0,
        ),
        elapsed_seconds=0.0,
        all_profiles_below_lock=grammar_percent < 97,
        fallback_profile_ids=fallback_profile_ids,
        native_script_runs=(),
    )


def _contextual_candidate(
    session: AdaptivePersistentOcrSession,
    *,
    text: str,
    grammar_percent: int,
    confidence: float,
) -> adaptive._Candidate:
    profile = next(profile for profile in session._profiles if profile.profile_id == session._DEFAULT_PROFILE_ID)
    return adaptive._Candidate(
        profile=profile,
        transform=adaptive.OcrTransform.CONTEXTUAL_COMPOSITE,
        output=_output(text, confidence),
        assessment=GrammarAssessment(
            grammar_percent,
            grammar_percent == 100,
            ("recursive-context-groups",),
        ),
        input_sha256="d" * 64,
        elapsed_seconds=0.0,
    )


def _two_unit_compaction(
    plan: BlockPlan,
    block_index: int = 0,
) -> BlockCompaction:
    return BlockCompaction(
        block_id=plan.blocks[block_index].block_id,
        placements=(
            CompactionPlacement(
                unit_id="unit-0",
                segment_ids=("segment-0",),
                source_bbox=Box(0, 0, 5, 10),
                crop_bbox=Box(0, 0, 5, 10),
            ),
            CompactionPlacement(
                unit_id="unit-1",
                segment_ids=("segment-1",),
                source_bbox=Box(5, 0, 10, 10),
                crop_bbox=Box(5, 0, 10, 10),
            ),
        ),
        omitted_empty_units=(),
        occupied_pixels_before=100,
        occupied_pixels_after=100,
        packed_canvas_pixels=100,
    )


def _local_plan(
    plan: BlockPlan,
    *,
    kind: str = "polar-local-full",
) -> BlockPlan:
    def local_block(block: RecognitionBlock) -> RecognitionBlock:
        segment_ids = block.segment_ids
        return replace(
            block,
            matrix_window=(0, 1, 0, len(segment_ids)),
            matrix_window_kind=kind,
            matrix_segment_shape=(1, len(segment_ids)),
            local_islands=(
                MatrixLocalIsland(
                    local_region_id="local-region-000000",
                    island_id="island-000000",
                    segment_ids=segment_ids,
                    region_boundary=(0, len(segment_ids)),
                    matrix_segment_shape=(1, len(segment_ids)),
                ),
            ),
            local_placements=tuple(
                MatrixLocalPlacement(
                    segment_id=segment_id,
                    local_region_id="local-region-000000",
                    island_id="island-000000",
                    polar_order=index,
                    region_order=index,
                    table_row=0,
                    table_column=index,
                    matrix_row=0,
                    matrix_column=index,
                )
                for index, segment_id in enumerate(segment_ids)
            ),
        )

    return replace(
        plan,
        blocks=tuple(local_block(block) for block in plan.blocks),
    )


def _compactions_for(plan: BlockPlan) -> tuple[BlockCompaction, ...]:
    return tuple(_two_unit_compaction(plan, index) for index in range(len(plan.blocks)))


def _isolated_recursive(
    candidate: adaptive._Candidate | None,
) -> adaptive._IsolatedRecursiveRecognition:
    return adaptive._IsolatedRecursiveRecognition(
        candidate=candidate,
        attempts=(),
        observations=(),
    )


def test_ineffective_local_split_is_learned_and_skipped(
    monkeypatch: object,
) -> None:
    plan, crops = _fixtures(tuple(_png(210 + index) for index in range(6)))
    plan = _local_plan(plan)
    with _session(monkeypatch, {}, max_workers=3) as session:
        full = tuple(
            _full_block_recognition(
                session,
                text="weak full block",
                grammar_percent=70,
                confidence=0.50,
            )
            for _ in plan.blocks
        )
        recursive = _contextual_candidate(
            session,
            text="weak contextual block",
            grammar_percent=71,
            confidence=0.55,
        )
        called = []

        def recognize(**kwargs):
            called.append(kwargs["block_id"])
            return _isolated_recursive(recursive)

        monkeypatch.setattr(session, "_recognize_recursive_isolated", recognize)
        results = session._recognize_recursive_blocks(
            plan=plan,
            crops=crops,
            compaction_by_id={item.block_id: item for item in _compactions_for(plan)},
            recognized=full,
        )

    assert sorted(called) == [block.block_id for block in plan.blocks[:3]]
    assert tuple(results) == (0, 1, 2)


def test_native_script_evidence_bypasses_local_split_learning(
    monkeypatch: object,
) -> None:
    plan, crops = _fixtures(tuple(_png(220 + index) for index in range(5)))
    plan = _local_plan(plan, kind="polar-local-signature")
    with _session(monkeypatch, {}, max_workers=3) as session:
        full = tuple(
            _full_block_recognition(
                session,
                text="русский текст",
                grammar_percent=70,
                confidence=0.50,
                fallback_profile_ids=("chi_sim",),
            )
            for _ in plan.blocks
        )
        recursive = _contextual_candidate(
            session,
            text="测试数据",
            grammar_percent=97,
            confidence=0.90,
        )
        called = []

        def recognize(**kwargs):
            called.append(kwargs["block_id"])
            return _isolated_recursive(recursive)

        monkeypatch.setattr(session, "_recognize_recursive_isolated", recognize)
        results = session._recognize_recursive_blocks(
            plan=plan,
            crops=crops,
            compaction_by_id={item.block_id: item for item in _compactions_for(plan)},
            recognized=full,
        )

    assert sorted(called) == [block.block_id for block in plan.blocks]
    assert tuple(results) == tuple(range(5))


def test_local_split_parallel_results_are_deterministic(
    monkeypatch: object,
) -> None:
    plan, crops = _fixtures(tuple(_png(230 + index) for index in range(6)))
    plan = _local_plan(plan)
    with _session(monkeypatch, {}, max_workers=3) as session:
        full = tuple(
            _full_block_recognition(
                session,
                text="weak full block",
                grammar_percent=70,
                confidence=0.50,
            )
            for _ in plan.blocks
        )
        barrier = threading.Barrier(3)
        active = 0
        peak = 0
        lock = threading.Lock()

        def recognize(**kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            if kwargs["block_id"] in {block.block_id for block in plan.blocks[:3]}:
                barrier.wait(timeout=2.0)
            with lock:
                active -= 1
            index = int(kwargs["block_id"].rsplit("-", 1)[1])
            candidate = _contextual_candidate(
                session,
                text=f"context {index}",
                grammar_percent=97,
                confidence=0.90,
            )
            return _isolated_recursive(candidate)

        monkeypatch.setattr(session, "_recognize_recursive_isolated", recognize)
        results = session._recognize_recursive_blocks(
            plan=plan,
            crops=crops,
            compaction_by_id={item.block_id: item for item in _compactions_for(plan)},
            recognized=full,
        )

    assert peak == 3
    assert tuple(results) == tuple(range(6))
    assert tuple(candidate.output.text for candidate in results.values()) == tuple(
        f"context {index}" for index in range(6)
    )


def test_local_split_learning_resets_for_new_object(
    monkeypatch: object,
) -> None:
    plan, crops = _fixtures(tuple(_png(240 + index) for index in range(4)))
    plan = _local_plan(plan)
    with _session(monkeypatch, {}, max_workers=3) as session:
        full = tuple(
            _full_block_recognition(
                session,
                text="weak full block",
                grammar_percent=70,
                confidence=0.50,
            )
            for _ in plan.blocks
        )
        recursive = _contextual_candidate(
            session,
            text="weak contextual block",
            grammar_percent=71,
            confidence=0.55,
        )
        calls = 0

        def recognize(**_kwargs):
            nonlocal calls
            calls += 1
            return _isolated_recursive(recursive)

        monkeypatch.setattr(session, "_recognize_recursive_isolated", recognize)
        kwargs = {
            "plan": plan,
            "crops": crops,
            "compaction_by_id": {item.block_id: item for item in _compactions_for(plan)},
            "recognized": full,
        }
        first = session._recognize_recursive_blocks(**kwargs)
        second = session._recognize_recursive_blocks(**kwargs)

    assert tuple(first) == (0, 1, 2)
    assert tuple(second) == (0, 1, 2)
    assert calls == 6


def test_ineffective_local_full_sweep_learns_primary_only(
    monkeypatch: object,
) -> None:
    payloads = tuple(_png(180 + index) for index in range(6))
    plan, crops = _fixtures(payloads)
    plan = _local_plan(plan)
    scores = {
        (profile_id, (10, 10), 180 + index): score
        for index in range(6)
        for profile_id, score in (
            ("rus-eng", 70),
            ("rus", 69),
            ("eng", 68),
            ("chi_sim", 20),
            ("ell", 30),
            ("equ", 40),
        )
    }
    with _session(monkeypatch, scores, max_workers=3) as session:
        recognized = session._recognize_blocks(plan=plan, crops=crops)
        last_profiles = _profile_order(
            plan.blocks[-1].block_id,
            "full-block",
            tuple(session.state.attempts),
        )

    assert len(recognized) == 6
    assert last_profiles == ("rus-eng",)
    assert all(
        item.candidate is not None and item.candidate.transform is adaptive.OcrTransform.RAW for item in recognized
    )


def test_low_quality_contextual_composite_is_not_acceptable(
    monkeypatch: object,
) -> None:
    with _session(monkeypatch, {}) as session:
        low = _contextual_candidate(
            session,
            text="mixed unreadable output",
            grammar_percent=96,
            confidence=0.84,
        )
        acceptable = _contextual_candidate(
            session,
            text="readable contextual output",
            grammar_percent=96,
            confidence=0.85,
        )

        assert session._contextual_composite_is_acceptable(low, ()) is False
        assert session._contextual_composite_is_acceptable(acceptable, ()) is True


def test_low_quality_contextual_composite_does_not_replace_full_block(
    monkeypatch: object,
) -> None:
    plan, crops = _fixtures((_png(201),))
    compaction = _two_unit_compaction(plan)
    with _session(monkeypatch, {}) as session:
        full = _full_block_recognition(
            session,
            text="full block evidence",
            grammar_percent=70,
            confidence=0.50,
        )
        recursive = _contextual_candidate(
            session,
            text="contextual garbage",
            grammar_percent=71,
            confidence=0.55,
        )
        monkeypatch.setattr(
            session,
            "_recognize_blocks",
            lambda **_kwargs: (full,),
        )
        monkeypatch.setattr(
            session,
            "_recognize_recursive_blocks",
            lambda **_kwargs: {0: recursive},
        )
        monkeypatch.setattr(
            session,
            "_should_recurse_full_block",
            lambda _full: True,
        )
        monkeypatch.setattr(
            session,
            "_map_full_output",
            lambda output, *_mapping_contract: output,
        )
        result = session.run_with_compaction(
            plan=plan,
            crops=crops,
            compactions=(compaction,),
        )

    assert result.jobs[0].transform is adaptive.OcrTransform.RAW
    assert result.jobs[0].output == full.candidate.output
    assert any("recursive=contextual-composite-rejected" in diagnostic for diagnostic in result.diagnostics)


def test_contextual_composite_output_is_not_mapped_twice(
    monkeypatch: object,
) -> None:
    plan, crops = _fixtures((_png(202),))
    compaction = _two_unit_compaction(plan)
    with _session(monkeypatch, {}) as session:
        full = _full_block_recognition(
            session,
            text="full block evidence",
            grammar_percent=70,
            confidence=0.50,
        )
        recursive = _contextual_candidate(
            session,
            text="accepted contextual output",
            grammar_percent=97,
            confidence=0.60,
        )
        monkeypatch.setattr(
            session,
            "_recognize_blocks",
            lambda **_kwargs: (full,),
        )
        monkeypatch.setattr(
            session,
            "_recognize_recursive_blocks",
            lambda **_kwargs: {0: recursive},
        )
        monkeypatch.setattr(
            session,
            "_should_recurse_full_block",
            lambda _full: True,
        )

        def unexpected_mapping(*_args, **_kwargs):
            raise AssertionError("contextual output was mapped twice")

        monkeypatch.setattr(session, "_map_full_output", unexpected_mapping)
        result = session.run_with_compaction(
            plan=plan,
            crops=crops,
            compactions=(compaction,),
        )

    assert result.jobs[0].transform is adaptive.OcrTransform.CONTEXTUAL_COMPOSITE
    assert result.jobs[0].output == recursive.output


def test_high_confidence_full_block_below_97_skips_membership_recursion(
    monkeypatch: object,
) -> None:
    with _session(monkeypatch, {}) as session:
        full_block = _full_block_recognition(
            session,
            text="Metric Value 7 8",
            grammar_percent=96,
            confidence=0.90,
        )

        assert session._should_recurse_full_block(full_block) is False


def test_low_confidence_full_block_recurses(
    monkeypatch: object,
) -> None:
    with _session(monkeypatch, {}) as session:
        full_block = _full_block_recognition(
            session,
            text="Metric Value 7 8",
            grammar_percent=96,
            confidence=0.84,
        )

        assert session._should_recurse_full_block(full_block) is True


def test_missing_evidenced_cjk_recurses_for_mixed_language_path(
    monkeypatch: object,
) -> None:
    with _session(monkeypatch, {}) as session:
        full_block = _full_block_recognition(
            session,
            text="Mixed language sample",
            grammar_percent=96,
            confidence=0.90,
            fallback_profile_ids=("chi_sim",),
        )

        assert session._should_recurse_full_block(full_block) is True


def test_visible_greek_and_math_evidence_do_not_force_recursion(
    monkeypatch: object,
) -> None:
    with _session(monkeypatch, {}) as session:
        full_block = _full_block_recognition(
            session,
            text="alpha α beta x = 2",
            grammar_percent=90,
            confidence=0.90,
            fallback_profile_ids=("ell", "equ"),
        )

        assert session._should_recurse_full_block(full_block) is False


def test_full_block_at_97_skips_recursion_even_with_low_confidence(
    monkeypatch: object,
) -> None:
    with _session(monkeypatch, {}) as session:
        full_block = _full_block_recognition(
            session,
            text="accepted context",
            grammar_percent=97,
            confidence=0.40,
            fallback_profile_ids=("chi_sim",),
        )

        assert session._should_recurse_full_block(full_block) is False


def test_context_group_cache_includes_ordered_units_policy_and_transform(
    monkeypatch: object,
) -> None:
    plan, crop, compaction = _many_unit_fixture(1)

    def score_hook(
        key: tuple[str, tuple[int, int], int],
        _payload: bytes,
    ) -> int:
        return {
            "rus-eng": 40,
            "chi_sim": 100,
            "eng": 90,
        }.get(key[0], 10)

    with _session(monkeypatch, {}) as session:
        _Worker.score_hook = score_hook
        _Worker.text_hook = lambda key, _payload: (
            "测试数据100" if key[0] == "chi_sim" else f"score{score_hook(key, _payload)}"
        )
        session.state.locked_profile_id = "rus-eng"
        primary_only = session._recursive_candidate(
            block_id="block-000000",
            block_bbox=plan.blocks[0].bbox,
            crop=crop,
            compaction=compaction,
            fallback_profile_ids=(),
        )
        chinese = session._recursive_candidate(
            block_id="block-000000",
            block_bbox=plan.blocks[0].bbox,
            crop=crop,
            compaction=compaction,
            fallback_profile_ids=("chi_sim",),
        )
        session.state.locked_profile_id = "eng"
        english = session._recursive_candidate(
            block_id="block-000000",
            block_bbox=plan.blocks[0].bbox,
            crop=crop,
            compaction=compaction,
            fallback_profile_ids=(),
        )

    assert primary_only is not None
    assert primary_only.profile.profile_id == "rus-eng"
    assert chinese is not None
    assert chinese.profile.profile_id == "chi_sim"
    assert english is not None
    assert english.profile.profile_id == "eng"
    assert {
        (
            unit_ids,
            locked_profile_id,
            fallback_policy,
            transform,
        )
        for (
            unit_ids,
            locked_profile_id,
            fallback_policy,
            transform,
        ) in session._group_candidate_cache
    } == {
        (("unit-0000",), "rus-eng", (), adaptive.OcrTransform.RAW),
        (
            ("unit-0000",),
            "rus-eng",
            ("chi_sim",),
            adaptive.OcrTransform.RAW,
        ),
        (("unit-0000",), "eng", (), adaptive.OcrTransform.RAW),
    }


def test_combined_profile_splays_engine_order_after_three_losses(
    monkeypatch: object,
) -> None:
    with _session(monkeypatch, {}) as session:
        _Worker.score_hook = lambda key, _payload: (90 if key[0] == "eng-rus" else 80)
        for label in (31, 32, 33):
            decision = session._recognize_image(
                block_id=f"block-{label}",
                unit_id="full-block",
                raw_png=_png(label),
                primary_profile_id="rus-eng",
                transforms=(adaptive.OcrTransform.RAW,),
                fallback_profile_ids=(),
                ordered_mixed_profile=True,
            )
            assert decision.candidate is not None
            assert decision.candidate.profile.profile_id == "rus-eng"
            assert decision.candidate.profile.languages == ("eng", "rus")

        before = len(_Worker.calls)
        session._recognize_image(
            block_id="block-34",
            unit_id="full-block",
            raw_png=_png(34),
            primary_profile_id="rus-eng",
            transforms=(adaptive.OcrTransform.RAW,),
            fallback_profile_ids=(),
            ordered_mixed_profile=True,
        )

    assert _Worker.calls[before:] == [("eng-rus", (10, 10), 34)]
    combined_attempts = tuple(attempt for attempt in session.state.attempts if attempt.profile_id == "rus-eng")
    assert {attempt.languages for attempt in combined_attempts} == {
        ("rus", "eng"),
        ("eng", "rus"),
    }


def test_homogeneous_mixed_profile_demotes_pure_language_fallbacks(
    monkeypatch: object,
) -> None:
    with _session(monkeypatch, {}) as session:
        session.state.locked_profile_id = "rus-eng"
        session.state.lock_is_provisional = False
        _Worker.score_hook = lambda key, _payload: {
            "eng-rus": 90,
            "rus-eng": 80,
            "rus": 70,
            "eng": 70,
        }.get(key[0], 10)
        for label in (41, 42, 43):
            session._recognize_image(
                block_id=f"block-{label}",
                unit_id="full-block",
                raw_png=_png(label),
                primary_profile_id="rus-eng",
                transforms=(adaptive.OcrTransform.RAW,),
                ordered_mixed_profile=True,
            )

        before = len(_Worker.calls)
        session._recognize_image(
            block_id="block-44",
            unit_id="full-block",
            raw_png=_png(44),
            primary_profile_id="rus-eng",
            transforms=(adaptive.OcrTransform.RAW,),
            ordered_mixed_profile=True,
        )

    assert _Worker.calls[before:] == [("eng-rus", (10, 10), 44)]
    assert session.state.fallback_loss_streaks == {"rus": 3, "eng": 3}


def test_parallel_local_calibration_applies_winner_before_second_phase(
    monkeypatch: object,
) -> None:
    plan, crops = _fixtures(tuple(_png(label) for label in range(61, 67)))
    plan = _local_plan(plan)
    with _session(monkeypatch, {}, max_workers=3) as session:
        session.state.locked_profile_id = "rus-eng"
        session.state.lock_is_provisional = False
        _Worker.score_hook = lambda key, _payload: {
            "eng-rus": 90,
            "rus-eng": 80,
            "rus": 70,
            "eng": 70,
        }.get(key[0], 10)
        session._recognize_blocks(plan=plan, crops=crops)

    calls = [key[0] for key in _Worker.calls]
    assert calls.count("eng-rus") == 6
    assert calls.count("rus-eng") == 3
    assert calls.count("rus") == 3
    assert calls.count("eng") == 3


def test_new_unsupported_unicode_resets_order_and_fallback_guards(
    monkeypatch: object,
) -> None:
    with _session(monkeypatch, {}) as session:
        session.state.locked_profile_id = "rus-eng"
        session.state.lock_is_provisional = False
        _Worker.score_hook = lambda key, _payload: {
            "eng-rus": 90,
            "rus-eng": 80,
            "rus": 70,
            "eng": 70,
            "chi_sim": 95,
        }.get(key[0], 10)
        for label in (51, 52, 53):
            session._recognize_image(
                block_id=f"block-{label}",
                unit_id="full-block",
                raw_png=_png(label),
                primary_profile_id="rus-eng",
                transforms=(adaptive.OcrTransform.RAW,),
                ordered_mixed_profile=True,
            )
        _Worker.text_hook = lambda key, _payload: (
            "\u6d4b\u8bd5\u6570\u636e95"
            if key[0] == "chi_sim"
            else (
                "\u4e2d\u6587score90"
                if key[0] == "eng-rus" and key[2] == 54
                else f"score{_Worker.score_hook(key, _payload)}"
            )
        )
        before = len(_Worker.calls)
        decision = session._recognize_image(
            block_id="block-54",
            unit_id="full-block",
            raw_png=_png(54),
            primary_profile_id="rus-eng",
            transforms=(adaptive.OcrTransform.RAW,),
            mixed_fallback_only=True,
            ordered_mixed_profile=True,
        )

    assert decision.candidate is not None
    assert {key[0] for key in _Worker.calls[before:]} == {
        "eng-rus",
        "rus-eng",
        "chi_sim",
    }
    assert decision.candidate.profile.profile_id == "chi_sim"


def test_profile_exhaustion_is_preserved_as_unresolved_complete_evidence(
    monkeypatch: object,
) -> None:
    plan, crops = _fixtures((_png(29),))

    def recognition_miss(
        _worker: _Worker,
        _png_bytes: bytes,
    ) -> OcrEngineOutput:
        raise RuntimeError("no observed text")

    monkeypatch.setattr(_Worker, "recognize", recognition_miss)
    with _session(monkeypatch, {}) as session:
        result = session.run(plan=plan, crops=crops)

    assert result.status is OcrQueueStatus.COMPLETE
    assert result.complete == 1
    assert result.failed == 0
    assert len(result.jobs) == 1
    job = result.jobs[0]
    assert job.status is OcrJobStatus.COMPLETE
    assert job.output == OcrEngineOutput(
        text="",
        words=(),
        geometry=OcrOutputGeometry.WORD_BOXES,
    )
    assert job.error_type is None
    assert job.error_message is None
    assert job.failure_code is None
    assert job.capability_id == "adaptive-language-unresolved"
    assert job.input_sha256 == job.context_sha256
    assert len(job.input_sha256) == 64
    assert ("block=block-000000;outcome=unresolved;" "reason=profile-exhausted") in result.diagnostics
    assert len(_Worker.calls) == 0
    assert {(attempt.profile_id, attempt.transform.value) for attempt in session.state.attempts} == {
        ("rus-eng", "raw"),
        ("rus-eng", "gamma"),
        ("rus", "raw"),
        ("eng", "raw"),
        ("chi_sim", "raw"),
        ("ell", "raw"),
        ("equ", "raw"),
    }
    assert all(attempt.unit_id == "full-block" for attempt in session.state.attempts)


def test_document_lock_survives_policy_runs_and_cache_remains_shared(
    monkeypatch: object,
) -> None:
    first_plan, first_crops = _fixtures((_png(31),))
    next_plan, next_crops = _fixtures((_png(32),))
    next_plan = replace(
        next_plan,
        diagnostics=("mode=overlapping-line-pairs",),
    )
    scores = {
        ("rus-eng", (10, 10), 32): 100,
    }
    with _session(monkeypatch, scores) as session:
        session.run(plan=first_plan, crops=first_crops)
        first_attempt_count = len(session.state.attempts)
        first_metrics = session.cache_metrics()

        session.run(plan=next_plan, crops=next_crops)
        second_attempt_count = len(session.state.attempts)
        second_metrics = session.cache_metrics()
        second_attempts = tuple(session.state.attempts[first_attempt_count:second_attempt_count])

        adapter_calls_before_repeat = len(_Worker.calls)
        session.run(plan=next_plan, crops=next_crops)
        third_metrics = session.cache_metrics()
        repeated_attempts = tuple(session.state.attempts[second_attempt_count:])

    assert session.state.locked_profile_id == "rus-eng"
    assert session.state.lock_is_provisional is True
    assert first_metrics == {
        "requests": 7,
        "hits": 0,
        "misses": 7,
        "waits": 0,
        "entries": 7,
        "exact_duplicate_calls_avoided": 0,
        "ocr_work_seconds": first_metrics["ocr_work_seconds"],
    }
    assert {attempt.profile_id for attempt in second_attempts} == {"rus-eng"}
    assert {attempt.transform.value for attempt in second_attempts} == {"raw"}
    assert second_metrics["requests"] == 8
    assert second_metrics["hits"] == 0
    assert second_metrics["misses"] == 8
    assert len(_Worker.calls) == adapter_calls_before_repeat
    assert {attempt.profile_id for attempt in repeated_attempts} == {"rus-eng"}
    assert {attempt.status for attempt in repeated_attempts} == {"cache-hit"}
    assert third_metrics["requests"] == 9
    assert third_metrics["hits"] == 1
    assert third_metrics["misses"] == 8


def _source_placement(
    unit_id: str,
    matrix_row: int,
    *,
    matrix_column: int = 0,
) -> adaptive.SourcePlacementArtifact:
    pixels = np.full((32, 120, 3), 255, dtype=np.uint8)
    for glyph_left in range(3, 108, 12):
        pixels[3:29, glyph_left : glyph_left + 4] = 16
        pixels[8:12, glyph_left : glyph_left + 9] = 16
        pixels[20:24, glyph_left : glyph_left + 9] = 16
    payload = adaptive._png_bytes(pixels)
    return adaptive.SourcePlacementArtifact(
        unit_id=unit_id,
        segment_ids=(unit_id,),
        source_bbox=Box(
            matrix_column * 140,
            matrix_row * 40,
            matrix_column * 140 + 120,
            matrix_row * 40 + 32,
        ),
        png_bytes=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        island_id="island-0",
        matrix_row=matrix_row,
        matrix_column=matrix_column,
        polar_order=matrix_row * 10 + matrix_column,
    )


def _source_leaf(
    artifact: adaptive.SourcePlacementArtifact,
    first_order: int = 0,
) -> adaptive._ContextGroup:
    canonical = np.full((32, 120, 3), 255, dtype=np.uint8)
    tile = adaptive._Tile(
        unit_id=artifact.unit_id,
        segment_ids=artifact.segment_ids,
        source_bbox=artifact.source_bbox,
        pixels=canonical,
    )
    return adaptive._ContextGroup(
        group_id=f"block:leaf:{artifact.unit_id}",
        depth=3,
        first_order=first_order,
        tiles=(tile,),
        png_bytes=adaptive._png_bytes(canonical),
        placements=(
            CompactionPlacement(
                unit_id=artifact.unit_id,
                segment_ids=artifact.segment_ids,
                source_bbox=artifact.source_bbox,
                crop_bbox=Box(0, 0, 120, 32),
            ),
        ),
    )


def test_source_placement_line_preserves_long_strokes_digest_and_mapping(
    monkeypatch: object,
) -> None:
    artifacts = tuple(_source_placement(f"unit-{row}", row) for row in range(4))
    leaf = _source_leaf(artifacts[0])
    with _session(monkeypatch, {}) as session:
        line = session._build_source_placement_line(
            block_id="block",
            leaf_group=leaf,
            source_placements=artifacts,
        )
        assert line is not None
        anchor = line.placements[0]
        output = OcrEngineOutput(
            text="中文数字",
            words=(OcrWord("中文数字", anchor.crop_bbox, 0.99),),
            geometry=OcrOutputGeometry.WORD_BOXES,
        )
        original = CompactionPlacement(
            unit_id=artifacts[0].unit_id,
            segment_ids=artifacts[0].segment_ids,
            source_bbox=artifacts[0].source_bbox,
            crop_bbox=Box(0, 0, 120, 32),
        )
        mapped = session._map_context_output(
            output,
            line.placements,
            {original.unit_id: original},
            Box(0, 0, 120, 32),
            allowed_unit_ids=line.emit_unit_ids,
        )
    with Image.open(io.BytesIO(line.png_bytes)) as opened:
        line_pixels = np.asarray(opened.convert("RGB"))

    assert hashlib.sha256(line.png_bytes).hexdigest()
    assert np.count_nonzero(np.all(line_pixels < 64, axis=2)) > 100
    assert line.emit_unit_ids == ("unit-0",)
    assert mapped.text == "中文数字"
    assert mapped.words[0].bbox == original.crop_bbox


def test_good_canonical_leaf_never_runs_source_fallback(
    monkeypatch: object,
) -> None:
    artifacts = tuple(_source_placement(f"unit-{row}", row) for row in range(4))
    leaf = _source_leaf(artifacts[0])
    scores = {("rus-eng", (120, 32), 255): 97}
    with _session(monkeypatch, scores) as session:
        session.state.locked_profile_id = "rus-eng"
        recognized = session._recognize_context_groups(
            block_id="block",
            block_bbox=Box(0, 0, 120, 32),
            groups=(leaf,),
            fallback_profile_ids=("chi_sim",),
            source_placements=artifacts,
        )

    assert len(recognized) == 1
    assert not any("source-placement-line" in attempt.unit_id for attempt in session.state.attempts)


def test_missing_native_script_runs_one_source_raw_and_skips_gamma(
    monkeypatch: object,
) -> None:
    artifacts = tuple(_source_placement(f"unit-{row}", row) for row in range(4))
    leaf = _source_leaf(artifacts[0])

    def recognize(worker: _Worker, png_bytes: bytes) -> OcrEngineOutput:
        with Image.open(io.BytesIO(png_bytes)) as opened:
            opened.load()
            width, height = opened.size
        profile_id = "-".join(worker.config.languages)
        if profile_id == "chi_sim" and width > 120:
            return OcrEngineOutput(
                text="中文数字 score97",
                words=(
                    OcrWord("中文数字", Box(4, 4, 124, height - 4), 0.99),
                    OcrWord("score97", Box(125, 4, min(width, 180), height - 4), 0.99),
                ),
                geometry=OcrOutputGeometry.WORD_BOXES,
            )
        raise RuntimeError("canonical specialized miss")

    monkeypatch.setattr(_Worker, "recognize", recognize)
    with _session(monkeypatch, {}) as session:
        session.state.locked_profile_id = "rus-eng"
        recognized = session._recognize_context_groups(
            block_id="block",
            block_bbox=Box(0, 0, 120, 32),
            groups=(leaf,),
            fallback_profile_ids=("chi_sim",),
            forced_native_search_group_ids=frozenset((leaf.group_id,)),
            source_placements=artifacts,
        )

    source_attempts = tuple(attempt for attempt in session.state.attempts if "source-placement-line" in attempt.unit_id)
    assert len(recognized) == 1
    assert recognized[0][0].source_fallback is True
    assert recognized[0][1].transform is adaptive.OcrTransform.SOURCE_PLACEMENT_FALLBACK
    assert len(source_attempts) == 1
    assert source_attempts[0].transform is adaptive.OcrTransform.SOURCE_PLACEMENT_FALLBACK


def test_source_line_builder_is_parallel_order_deterministic(
    monkeypatch: object,
) -> None:
    artifacts = tuple(_source_placement(f"unit-{row}", row) for row in range(4))
    leaves = tuple(_source_leaf(artifact, first_order=index) for index, artifact in enumerate(artifacts[:2]))

    def execute(max_workers: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
        with _session(monkeypatch, {}, max_workers=max_workers) as session:
            lines = tuple(
                session._build_source_placement_line(
                    block_id="block",
                    leaf_group=leaf,
                    source_placements=artifacts,
                )
                for leaf in leaves
            )
        assert all(line is not None for line in lines)
        return (
            tuple(line.group_id for line in lines if line is not None),
            tuple(hashlib.sha256(line.png_bytes).hexdigest() for line in lines if line is not None),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        parallel = tuple(executor.map(execute, (1, 2)))

    assert parallel[0] == parallel[1]


def test_source_line_candidate_cache_resolves_parallel_key_once() -> None:
    cache = adaptive._SourceLineCandidateCache()
    barrier = threading.Barrier(2)
    calls = 0
    lock = threading.Lock()

    def operation() -> None:
        nonlocal calls
        with lock:
            calls += 1
        return None

    def resolve() -> None:
        barrier.wait(timeout=2.0)
        return cache.resolve(
            ("same-source-digest", "chi_sim", adaptive.OcrTransform.RAW),
            operation,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: resolve(), range(2)))

    assert results == (None, None)
    assert calls == 1


def test_specialized_bbox_routes_only_intersecting_source_placement(
    monkeypatch: object,
) -> None:
    artifacts = tuple(_source_placement(f"unit-{index}", index) for index in range(2))
    compaction = BlockCompaction(
        block_id="block",
        placements=(
            CompactionPlacement(
                unit_id="unit-0",
                segment_ids=("segment-0",),
                source_bbox=artifacts[0].source_bbox,
                crop_bbox=Box(0, 0, 40, 32),
            ),
            CompactionPlacement(
                unit_id="unit-1",
                segment_ids=("segment-1",),
                source_bbox=artifacts[1].source_bbox,
                crop_bbox=Box(48, 0, 88, 32),
            ),
        ),
        omitted_empty_units=(),
        occupied_pixels_before=0,
        occupied_pixels_after=0,
        packed_canvas_pixels=0,
        source_placements=artifacts,
    )
    with _session(monkeypatch, {}) as session:
        profile = next(profile for profile in session._profiles if profile.profile_id == "chi_sim")
        output = _output("中文", 0.8)
        output = adaptive.replace(
            output,
            words=(
                adaptive.replace(
                    output.words[0],
                    bbox=Box(52, 4, 84, 28),
                ),
            ),
        )
        candidate = adaptive._Candidate(
            profile=profile,
            transform=adaptive.OcrTransform.RAW,
            output=output,
            assessment=GrammarAssessment(40, False, ()),
            input_sha256="e" * 64,
            elapsed_seconds=0.0,
        )
        routed = session._native_script_unit_ids(
            (candidate,),
            ("chi_sim",),
            compaction,
        )

    assert routed == ("unit-1",)


def test_routed_native_bbox_skips_unattributed_leaf_source_fallback(
    monkeypatch: object,
) -> None:
    artifacts = tuple(_source_placement(f"unit-{index}", index) for index in range(4))
    leaves = tuple(_source_leaf(artifact, first_order=index) for index, artifact in enumerate(artifacts[:2]))
    source_calls = []

    with _session(monkeypatch, {}, max_workers=2) as session:
        session.state.locked_profile_id = "rus-eng"
        _Worker.score_hook = lambda _key, _payload: 96
        _Worker.text_hook = lambda key, _payload: ("" if key[0] == "chi_sim" else "latin")

        def source_candidate(**kwargs: object) -> None:
            group = kwargs["group"]
            assert isinstance(group, adaptive._ContextGroup)
            source_calls.append(group.emit_unit_ids)
            return None

        monkeypatch.setattr(
            session,
            "_source_line_candidate",
            source_candidate,
        )
        session._recognize_context_groups(
            block_id="block",
            block_bbox=Box(0, 0, 120, 64),
            groups=leaves,
            fallback_profile_ids=("chi_sim",),
            forced_native_search_group_ids=frozenset(leaf.group_id for leaf in leaves),
            routed_native_unit_ids=frozenset(("unit-0",)),
            source_placements=artifacts,
        )

    assert source_calls == [("unit-0",)]


def _topology_artifact(
    unit_id: str,
    bbox: Box,
    *,
    row: int,
    column: int,
    island_id: str = "island-0",
) -> adaptive.SourcePlacementArtifact:
    payload = adaptive._png_bytes(np.full((bbox.height, bbox.width, 3), 255, dtype=np.uint8))
    return adaptive.SourcePlacementArtifact(
        unit_id=unit_id,
        segment_ids=(f"segment-{unit_id}",),
        source_bbox=bbox,
        png_bytes=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        island_id=island_id,
        matrix_row=row,
        matrix_column=column,
        polar_order=row * 10 + column,
    )


def _topology_queue_fixture() -> tuple[
    adaptive.BlockPlan,
    tuple[adaptive.BlockCompaction, ...],
    adaptive.OcrQueueResult,
    adaptive.TopologyScriptEvidence,
]:
    artifacts = (
        _topology_artifact("merged", Box(0, 0, 300, 40), row=0, column=0),
        _topology_artifact("native", Box(100, 50, 200, 90), row=1, column=1),
        _topology_artifact("unrelated", Box(310, 0, 400, 40), row=0, column=3),
    )
    block = adaptive.RecognitionBlock(
        block_id="block-000000",
        bbox=Box(0, 0, 400, 100),
        core_segment_ids=tuple(item.segment_ids[0] for item in artifacts),
        segment_ids=tuple(item.segment_ids[0] for item in artifacts),
        context_segment_ids=(),
        object_ids=("object-0",),
    )
    plan = adaptive.BlockPlan(
        aligned_size=(400, 100),
        source_segment_ids=block.segment_ids,
        blocks=(block,),
        adjacent_algebra=(),
    )
    compaction = adaptive.BlockCompaction(
        block_id=block.block_id,
        placements=tuple(
            adaptive.CompactionPlacement(
                unit_id=item.unit_id,
                segment_ids=item.segment_ids,
                source_bbox=item.source_bbox,
                crop_bbox=item.source_bbox,
            )
            for item in artifacts
        ),
        omitted_empty_units=(),
        occupied_pixels_before=0,
        occupied_pixels_after=0,
        packed_canvas_pixels=40_000,
        source_placements=artifacts,
    )
    output = OcrEngineOutput(
        text="РАЗДЕЛ FAX score70 中文 score99 noise score70",
        words=(
            OcrWord("РАЗДЕЛ FAX score70", Box(0, 0, 300, 40), 0.70),
            OcrWord("中文", Box(100, 50, 200, 90), 0.99),
            OcrWord("score99", Box(100, 50, 200, 90), 0.99),
            OcrWord("noise score70", Box(310, 0, 400, 40), 0.70),
        ),
        geometry=OcrOutputGeometry.WORD_BOXES,
    )
    job = adaptive.OcrJobResult(
        job_id="ocr-job-00000000",
        block_id=block.block_id,
        transform=adaptive.OcrTransform.RAW,
        lane_id="rus-eng",
        resource=adaptive.OcrResource.CPU,
        status=adaptive.OcrJobStatus.COMPLETE,
        output=output,
        error_type=None,
        error_message=None,
        elapsed_seconds=0.0,
        input_sha256="b" * 64,
        context_sha256="b" * 64,
    )
    queue = adaptive.OcrQueueResult(
        jobs=(job,),
        status=adaptive.OcrQueueStatus.COMPLETE,
        complete=1,
        failed=0,
    )
    evidence = adaptive.TopologyScriptEvidence(
        script_kind="cjk",
        matrix_sha256=adaptive._cache_fingerprint((plan.aligned_size, plan.source_segment_ids)),
        island_id="island-0",
        matrix_columns=(1,),
        source_left_ppm=250_000,
        source_right_ppm=500_000,
        confidence=0.99,
    )
    return plan, (compaction,), queue, evidence


def test_topology_routes_merged_and_second_pass_header_footer_deterministically(
    monkeypatch: object,
) -> None:
    plan, compactions, queue, evidence = _topology_queue_fixture()
    with _session(monkeypatch, {}) as session:
        merged = session._topology_route_candidates(
            plan=plan,
            compactions=compactions,
            queue=queue,
            evidence=(evidence,),
        )

        def external_route(unit_id: str, matrix_digest: str) -> tuple:
            artifact = _topology_artifact(
                unit_id,
                Box(0, 0, 400, 40),
                row=0,
                column=0,
                island_id=f"spatial:{unit_id}",
            )
            block = adaptive.RecognitionBlock(
                block_id="block-000000",
                bbox=Box(0, 0, 400, 40),
                core_segment_ids=artifact.segment_ids,
                segment_ids=artifact.segment_ids,
                context_segment_ids=(),
                object_ids=("object-0",),
            )
            external_plan = adaptive.BlockPlan(
                aligned_size=(400, 40),
                source_segment_ids=artifact.segment_ids,
                blocks=(block,),
                adjacent_algebra=(),
            )
            compaction = adaptive.BlockCompaction(
                block_id=block.block_id,
                placements=(
                    adaptive.CompactionPlacement(
                        unit_id=artifact.unit_id,
                        segment_ids=artifact.segment_ids,
                        source_bbox=artifact.source_bbox,
                        crop_bbox=artifact.source_bbox,
                    ),
                ),
                omitted_empty_units=(),
                occupied_pixels_before=0,
                occupied_pixels_after=0,
                packed_canvas_pixels=16_000,
                source_placements=(artifact,),
            )
            text = "header FAX score70" if unit_id == "header" else "footer FAX score70"
            output = OcrEngineOutput(
                text=text,
                words=(OcrWord(text, artifact.source_bbox, 0.70),),
                geometry=OcrOutputGeometry.WORD_BOXES,
            )
            job = adaptive.replace(
                queue.jobs[0],
                block_id=block.block_id,
                output=output,
            )
            external_queue = adaptive.replace(queue, jobs=(job,))
            forward = session._topology_route_candidates(
                plan=external_plan,
                compactions=(compaction,),
                queue=external_queue,
                evidence=(evidence,),
            )
            reverse = session._topology_route_candidates(
                plan=external_plan,
                compactions=(compaction,),
                queue=external_queue,
                evidence=tuple(reversed((evidence,))),
            )
            assert forward == reverse
            return forward

        header = external_route("header", "c" * 64)
        footer = external_route("footer", "d" * 64)

    assert merged == (("block-000000", "merged", ("cjk",)),)
    assert header == (("block-000000", "header", ("cjk",)),)
    assert footer == (("block-000000", "footer", ("cjk",)),)


def test_topology_does_not_route_unrelated_or_good_ordinary_cell(
    monkeypatch: object,
) -> None:
    plan, compactions, queue, evidence = _topology_queue_fixture()
    with _session(monkeypatch, {}) as session:
        routed = session._topology_route_candidates(
            plan=plan,
            compactions=compactions,
            queue=queue,
            evidence=(evidence,),
        )

    assert {unit_id for _block, unit_id, _scripts in routed} == {"merged"}


def test_topology_fusion_preserves_order_and_deduplicates_observed_spans(
    monkeypatch: object,
) -> None:
    with _session(monkeypatch, {}) as session:
        combined_profile = next(item for item in session._profiles if item.profile_id == "rus-eng")
        native_profile = next(item for item in session._profiles if item.profile_id == "chi_sim")
        combined = adaptive._Candidate(
            profile=combined_profile,
            transform=adaptive.OcrTransform.RAW,
            output=OcrEngineOutput(
                text="SECTION FAX tail",
                words=(
                    OcrWord("SECTION", Box(0, 0, 60, 30), 0.98),
                    OcrWord("FAX", Box(70, 0, 120, 30), 0.60),
                    OcrWord("tail", Box(130, 0, 180, 30), 0.98),
                ),
                geometry=OcrOutputGeometry.WORD_BOXES,
            ),
            assessment=GrammarAssessment(80, False, ()),
            input_sha256="1" * 64,
            elapsed_seconds=0.0,
        )
        native = adaptive._Candidate(
            profile=native_profile,
            transform=adaptive.OcrTransform.RAW,
            output=OcrEngineOutput(
                text="中文 中文",
                words=(
                    OcrWord("中文", Box(72, 0, 118, 30), 0.99),
                    OcrWord("中文", Box(72, 0, 118, 30), 0.99),
                ),
                geometry=OcrOutputGeometry.WORD_BOXES,
            ),
            assessment=GrammarAssessment(80, False, ()),
            input_sha256="2" * 64,
            elapsed_seconds=0.0,
        )
        fused = session._fuse_topology_source_words(
            anchor_bbox=Box(0, 0, 200, 30),
            combined=combined,
            native_candidates=(("cjk", native),),
        )

    assert fused is not None
    assert tuple(item[0].text for item in fused) == ("SECTION", "中文", "tail")
    assert tuple(item[1] for item in fused) == ("combined", "cjk", "combined")
