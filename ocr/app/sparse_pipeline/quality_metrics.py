from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_MINIMUM_ACCURACY_PERCENT = 91.0
_DEFAULT_MAX_EDIT_WORK = 16_000_000
_EDIT_WORD_BITS = 64


def compact_unicode_whitespace(value: str) -> str:
    """Remove exactly the codepoints classified as whitespace by Python."""

    if type(value) is not str:
        raise TypeError("metric input must be a string")
    return "".join(character for character in value if not character.isspace())


def exact_levenshtein(
    left: str,
    right: str,
    *,
    max_cells: int = _DEFAULT_MAX_EDIT_WORK,
) -> int:
    """Return exact codepoint Levenshtein loss with bounded bit-vector work.

    ``max_cells`` is retained as the public compatibility name, but it now
    bounds 64-bit-equivalent Myers work units instead of cells in a quadratic
    dynamic-programming matrix.  The previous row-by-row implementation made
    correct OCR output for long documents unscorable even though it used
    linear memory.  Python's arbitrary-width integers execute the bit-vector
    recurrence in C while preserving exact unit-cost insert/delete/substitute
    semantics for Unicode codepoints.
    """

    if type(left) is not str or type(right) is not str:
        raise TypeError("metric input must be a string")
    if type(max_cells) is not int or max_cells < 1:
        raise ValueError("metric max_cells must be a positive integer")

    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    # Removing common edges is exact and often turns a long near-match into a
    # small edit core before the bounded bit-vector alignment begins.
    prefix = 0
    common_length = min(len(left), len(right))
    while prefix < common_length and left[prefix] == right[prefix]:
        prefix += 1
    left_stop = len(left)
    right_stop = len(right)
    while left_stop > prefix and right_stop > prefix and left[left_stop - 1] == right[right_stop - 1]:
        left_stop -= 1
        right_stop -= 1
    left = left[prefix:left_stop]
    right = right[prefix:right_stop]
    if not left:
        return len(right)
    if not right:
        return len(left)

    # The bit pattern is the shorter string.  One update touches that pattern
    # in machine-word-sized chunks, so this estimate tracks the actual
    # algorithm instead of the discarded O(n*m) matrix.
    if len(left) > len(right):
        left, right = right, left
    word_columns = (len(left) + _EDIT_WORD_BITS - 1) // _EDIT_WORD_BITS
    work = len(right) * word_columns
    if work > max_cells:
        raise ValueError("exact metric alignment exceeds configured bit-vector work limit: " f"{work} > {max_cells}")

    character_masks: dict[str, int] = {}
    for index, character in enumerate(left):
        character_masks[character] = character_masks.get(character, 0) | (1 << index)

    width = len(left)
    full_mask = (1 << width) - 1
    high_bit = 1 << (width - 1)
    positive = full_mask
    negative = 0
    distance = width
    for character in right:
        equal = character_masks.get(character, 0)
        vertical = equal | negative
        horizontal = ((((equal & positive) + positive) ^ positive) | equal) & full_mask
        positive_horizontal = (negative | ~(horizontal | positive)) & full_mask
        negative_horizontal = positive & horizontal
        if positive_horizontal & high_bit:
            distance += 1
        elif negative_horizontal & high_bit:
            distance -= 1
        positive_horizontal = ((positive_horizontal << 1) | 1) & full_mask
        negative_horizontal = (negative_horizontal << 1) & full_mask
        positive = (negative_horizontal | ~(vertical | positive_horizontal)) & full_mask
        negative = positive_horizontal & vertical
    return distance


def exact_text_metric(
    reference: str,
    recognized: str,
    *,
    max_cells: int = _DEFAULT_MAX_EDIT_WORK,
) -> tuple[int, int, int, float]:
    """Score exact codepoints after removing only Unicode whitespace."""

    compact_reference = compact_unicode_whitespace(reference)
    compact_recognized = compact_unicode_whitespace(recognized)
    loss = exact_levenshtein(
        compact_reference,
        compact_recognized,
        max_cells=max_cells,
    )
    reference_characters = len(compact_reference)
    accuracy = (
        (100.0 if loss == 0 else 0.0)
        if reference_characters == 0
        else 100.0 * max(0.0, 1.0 - loss / reference_characters)
    )
    return loss, reference_characters, len(compact_recognized), accuracy


@dataclass(frozen=True)
class QualityGatePolicy:
    minimum_accuracy_percent: float = DEFAULT_MINIMUM_ACCURACY_PERCENT
    require_full_scoring: bool = True
    require_resolved: bool = True

    def __post_init__(self) -> None:
        value = self.minimum_accuracy_percent
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 100.0
        ):
            raise ValueError("minimum accuracy percent must be finite and in [0, 100]")
        if type(self.require_full_scoring) is not bool:
            raise TypeError("require_full_scoring must be a bool")
        if type(self.require_resolved) is not bool:
            raise TypeError("require_resolved must be a bool")

    @property
    def mode(self) -> str:
        return "strict" if self.require_full_scoring and self.require_resolved else "exploratory"


@dataclass(frozen=True)
class QualityGateDecision:
    status: str
    reasons: tuple[str, ...]
    full_scoring: bool
    zero_unresolved: bool
    accuracy_satisfied: bool


def evaluate_quality_gate(
    *,
    images: int,
    scored_images: int,
    failures: int,
    unresolved: int,
    micro_accuracy_percent: float | None,
    exact_execution_order: bool,
    all_artifacts: bool,
    policy: QualityGatePolicy,
) -> QualityGateDecision:
    """Evaluate a strict gate and distinguish relaxed exploratory acceptance."""

    for name, value in (
        ("images", images),
        ("scored_images", scored_images),
        ("failures", failures),
        ("unresolved", unresolved),
    ):
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if scored_images > images:
        raise ValueError("scored_images cannot exceed images")
    if type(exact_execution_order) is not bool or type(all_artifacts) is not bool:
        raise TypeError("gate invariants must be bool values")
    if not isinstance(policy, QualityGatePolicy):
        raise TypeError("policy must be a QualityGatePolicy")
    if micro_accuracy_percent is not None and (
        isinstance(micro_accuracy_percent, bool)
        or not isinstance(micro_accuracy_percent, (int, float))
        or not math.isfinite(float(micro_accuracy_percent))
        or not 0.0 <= float(micro_accuracy_percent) <= 100.0
    ):
        raise ValueError("micro accuracy percent must be finite and in [0, 100]")

    full_scoring = images > 0 and scored_images == images
    zero_unresolved = unresolved == 0
    accuracy_satisfied = micro_accuracy_percent is not None and float(micro_accuracy_percent) >= float(
        policy.minimum_accuracy_percent
    )
    mandatory = images > 0 and failures == 0 and exact_execution_order and all_artifacts
    strict_accepted = mandatory and full_scoring and zero_unresolved and accuracy_satisfied
    exploratory_accuracy = accuracy_satisfied or (not policy.require_full_scoring and micro_accuracy_percent is None)
    policy_accepted = (
        mandatory
        and (full_scoring or not policy.require_full_scoring)
        and (zero_unresolved or not policy.require_resolved)
        and exploratory_accuracy
    )

    reasons: list[str] = []
    if images == 0:
        reasons.append("no-images")
    if failures:
        reasons.append("execution-failures")
    if not exact_execution_order:
        reasons.append("execution-order-mismatch")
    if not all_artifacts:
        reasons.append("stage7-artifacts-missing")
    if not full_scoring:
        reasons.append("reference-scoring-incomplete")
    if not zero_unresolved:
        reasons.append("unresolved-items")
    if micro_accuracy_percent is None:
        reasons.append("micro-accuracy-unavailable")
    elif not accuracy_satisfied:
        reasons.append("micro-accuracy-below-threshold")

    return QualityGateDecision(
        status=("GREEN" if strict_accepted else "EXPLORATORY" if policy_accepted else "RED"),
        reasons=tuple(reasons),
        full_scoring=full_scoring,
        zero_unresolved=zero_unresolved,
        accuracy_satisfied=accuracy_satisfied,
    )


__all__ = [
    "DEFAULT_MINIMUM_ACCURACY_PERCENT",
    "QualityGateDecision",
    "QualityGatePolicy",
    "compact_unicode_whitespace",
    "evaluate_quality_gate",
    "exact_levenshtein",
    "exact_text_metric",
]
