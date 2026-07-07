from __future__ import annotations

from itertools import combinations

MERGE_UP_CODE = 3
MERGE_LEFT_CODE = 5
EMPTY_SLOT_CODE = 11
MERGE_BOTH_CODE = MERGE_UP_CODE + MERGE_LEFT_CODE

SPARSE_SIGNALS = (
    MERGE_UP_CODE,
    MERGE_LEFT_CODE,
    EMPTY_SLOT_CODE,
)


def _components_by_code() -> dict[int, frozenset[int]]:
    result = {0: frozenset()}
    for size in range(1, len(SPARSE_SIGNALS) + 1):
        for signals in combinations(SPARSE_SIGNALS, size):
            result[sum(signals)] = frozenset(signals)
    return result


SPARSE_CODE_COMPONENTS = _components_by_code()
SPARSE_SHADOW_CODES = frozenset(
    code
    for code, signals in SPARSE_CODE_COMPONENTS.items()
    if signals
)
MERGE_UP_CODES = frozenset(
    code
    for code, signals in SPARSE_CODE_COMPONENTS.items()
    if MERGE_UP_CODE in signals
)
MERGE_LEFT_CODES = frozenset(
    code
    for code, signals in SPARSE_CODE_COMPONENTS.items()
    if MERGE_LEFT_CODE in signals
)
MERGE_UP_ONLY_CODES = frozenset(
    code
    for code, signals in SPARSE_CODE_COMPONENTS.items()
    if MERGE_UP_CODE in signals and MERGE_LEFT_CODE not in signals
)
MERGE_BOTH_CODES = frozenset(MERGE_UP_CODES & MERGE_LEFT_CODES)
EMPTY_SLOT_CODES = frozenset(
    code
    for code, signals in SPARSE_CODE_COMPONENTS.items()
    if EMPTY_SLOT_CODE in signals
)


def sparse_code_components(code: int) -> frozenset[int]:
    return SPARSE_CODE_COMPONENTS.get(code, frozenset())


def has_sparse_signal(code: int, signal: int) -> bool:
    return signal in sparse_code_components(code)


def add_sparse_signal(code: int, signal: int) -> int:
    if signal not in SPARSE_SIGNALS:
        raise ValueError(f"Unknown sparse signal: {signal}")
    components = set(sparse_code_components(code))
    if code and not components:
        raise ValueError(f"Unknown sparse code: {code}")
    components.add(signal)
    return sum(components)
