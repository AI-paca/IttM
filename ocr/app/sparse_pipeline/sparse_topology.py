"""Numeric topology for ragged sparse document matrices.

Every discovered region is materialized, including an empty region, which
uses code ``7``. ``None`` is allowed only after the final real region: it is a
tail-compression marker instructing a reader to repeat the last numeric value
through the logical end of that row. It can never replace a leading or
internal empty region. Connections are additive and remain inspectable
without semantic object classification.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Hashable

MERGE_UP_CODE = 3
MERGE_LEFT_CODE = 5
EMPTY_SLOT_CODE = 7
MERGE_BOTH_CODE = MERGE_UP_CODE + MERGE_LEFT_CODE
SPARSE_SIGNALS = (MERGE_UP_CODE, MERGE_LEFT_CODE, EMPTY_SLOT_CODE)


def _components_by_code() -> dict[int, frozenset[int]]:
    result = {0: frozenset()}
    for size in range(1, len(SPARSE_SIGNALS) + 1):
        for signals in combinations(SPARSE_SIGNALS, size):
            result[sum(signals)] = frozenset(signals)
    return result


SPARSE_CODE_COMPONENTS = _components_by_code()


def sparse_code_components(code: int) -> frozenset[int]:
    """Decode the production 3/5/7 additive topology alphabet."""

    return SPARSE_CODE_COMPONENTS.get(code, frozenset())


@dataclass(frozen=True)
class EmptySlot:
    """A discovered cell with no payload; unlike ``None`` it is stored."""


EMPTY = EmptySlot()
TopologySlot = Hashable | EmptySlot | None


@dataclass(frozen=True)
class SpatialSlot:
    """One discovered horizontal interval in a local sparse row."""

    start: int
    end: int
    value: Hashable | EmptySlot


@dataclass(frozen=True)
class ObservedTopologyRow:
    """Literal coordinates discovered in one logical horizontal row.

    ``payload_columns`` are geometry cells carrying foreground.
    ``merge_left_columns`` is explicit horizontal-continuity evidence.
    ``empty_columns`` are equally real geometry cells without foreground.
    Every coordinate through the last discovered boundary must be present in
    exactly one of payload/empty. Coordinates after that boundary are the
    compressed ``null`` tail: the last value repeats to the row end.
    """

    payload_columns: tuple[int, ...]
    merge_left_columns: tuple[int, ...] = ()
    empty_columns: tuple[int, ...] = ()


@dataclass(frozen=True)
class TopologyEntry:
    """One materialized coordinate of the numeric sparse topology."""

    row: int
    column: int
    code: int
    empty: bool


def compose_sparse_code(
    *,
    merge_up: bool = False,
    merge_left: bool = False,
    empty: bool = False,
) -> int:
    """Compose the canonical additive code for one discovered coordinate."""

    if type(merge_up) is not bool:
        raise TypeError("merge_up must be a boolean")
    if type(merge_left) is not bool:
        raise TypeError("merge_left must be a boolean")
    if type(empty) is not bool:
        raise TypeError("empty must be a boolean")
    return (
        (MERGE_UP_CODE if merge_up else 0)
        + (MERGE_LEFT_CODE if merge_left else 0)
        + (EMPTY_SLOT_CODE if empty else 0)
    )


def encode_ragged_topology(
    rows: tuple[tuple[TopologySlot, ...], ...],
) -> tuple[tuple[int | None, ...], ...]:
    """Encode geometry components while preserving explicit empty and null.

    Equal hashable values describe one geometry-connected region.  This is not
    a semantic object label: for example, all cells joined by one ruled table
    network may share a value before Stage 6 decides that it is a table.
    ``EMPTY`` continues vertically through another ``EMPTY`` as ``10`` but
    never creates a horizontal merge across the separating gap. ``None`` is
    accepted only as a trailing compression marker and is omitted from the
    returned numeric prefix; a decoder repeats the last numeric code.
    """

    if type(rows) is not tuple or any(type(row) is not tuple for row in rows):
        raise TypeError("rows must be an immutable tuple of tuples")
    for row in rows:
        null_seen = False
        for slot in row:
            if slot is None:
                if not row or row[0] is None:
                    raise ValueError("null tail requires a preceding segment")
                null_seen = True
            elif null_seen:
                raise ValueError("null is allowed only in the row tail")
    width = max(map(len, rows), default=0)
    values: list[tuple[int | None, ...]] = []
    for row_index, row in enumerate(rows):
        encoded: list[int | None] = []
        for column in range(width):
            slot = row[column] if column < len(row) else None
            if slot is None:
                encoded.append(None)
                continue
            above = (
                rows[row_index - 1][column]
                if row_index > 0 and column < len(rows[row_index - 1])
                else None
            )
            left = row[column - 1] if column > 0 else None
            is_empty = isinstance(slot, EmptySlot)
            merge_up = above is not None and (
                (is_empty and isinstance(above, EmptySlot))
                or (not is_empty and slot == above)
            )
            merge_left = (
                not is_empty
                and left is not None
                and not isinstance(left, EmptySlot)
                and slot == left
            )
            encoded.append(
                compose_sparse_code(
                    merge_up=merge_up,
                    merge_left=merge_left,
                    empty=is_empty,
                )
            )
        while encoded and encoded[-1] is None:
            encoded.pop()
        values.append(tuple(encoded))
    return tuple(values)


def encode_spatial_topology(
    rows: tuple[tuple[SpatialSlot, ...], ...],
) -> tuple[tuple[int, ...], ...]:
    """Encode ragged rows using actual horizontal overlap for ``merge_up``.

    Unlike a rectangular array, a wide merged cell can overlap several cells
    in the row above or below it. A row containing one wide cell serializes
    one number; ``null`` then repeats that last value through any finer
    logical columns introduced by other rows.
    """

    if type(rows) is not tuple or any(type(row) is not tuple for row in rows):
        raise TypeError("rows must be an immutable tuple of tuples")
    result: list[tuple[int, ...]] = []
    previous_row: tuple[SpatialSlot, ...] = ()
    for row in rows:
        previous_end: int | None = None
        for slot in row:
            if not isinstance(slot, SpatialSlot):
                raise TypeError("each row item must be a SpatialSlot")
            if (
                type(slot.start) is not int
                or type(slot.end) is not int
                or slot.start >= slot.end
                or (previous_end is not None and slot.start < previous_end)
            ):
                raise ValueError("spatial slots must be ordered positive intervals")
            previous_end = slot.end

        encoded: list[int] = []
        for column, slot in enumerate(row):
            is_empty = isinstance(slot.value, EmptySlot)
            merge_up = any(
                max(slot.start, above.start) < min(slot.end, above.end)
                and (
                    (is_empty and isinstance(above.value, EmptySlot))
                    or (
                        not is_empty
                        and not isinstance(above.value, EmptySlot)
                        and slot.value == above.value
                    )
                )
                for above in previous_row
            )
            left = row[column - 1] if column > 0 else None
            merge_left = (
                not is_empty
                and left is not None
                and not isinstance(left.value, EmptySlot)
                and slot.value == left.value
            )
            encoded.append(
                compose_sparse_code(
                    merge_up=merge_up,
                    merge_left=merge_left,
                    empty=is_empty,
                )
            )
        result.append(tuple(encoded))
        previous_row = row
    return tuple(result)


def encode_observed_topology(
    rows: tuple[ObservedTopologyRow, ...],
) -> tuple[TopologyEntry, ...]:
    """Scan observed rows top-to-bottom and cells left-to-right.

    This is the canonical 0/3/5/7 methodology. Vertical continuity is derived
    only from a materialized coordinate in the immediately preceding logical
    row. Horizontal continuity is never guessed from proximity: it requires
    explicit ``merge_left_columns`` evidence and an occupied immediate-left
    neighbor. An explicit empty coordinate can continue vertically
    (7 + 3 = 10), but cannot merge horizontally or be crossed by a merge.
    Internal and leading gaps are forbidden because a line-bounded empty area
    is still a segment and must be supplied through ``empty_columns``. Only a
    trailing run may be omitted; its last code is repeated by the serializer's
    ``null`` tail convention.
    """

    if type(rows) is not tuple or any(
        not isinstance(row, ObservedTopologyRow) for row in rows
    ):
        raise TypeError("rows must be an immutable tuple of observed rows")
    previous_payload: frozenset[int] = frozenset()
    previous_empty: frozenset[int] = frozenset()
    result: list[TopologyEntry] = []
    for row_index, row in enumerate(rows):
        payload = _canonical_columns(row.payload_columns, name="payload")
        merge_left = _canonical_columns(
            row.merge_left_columns,
            name="merge-left",
        )
        empty = _canonical_columns(row.empty_columns, name="empty")
        payload_set = frozenset(payload)
        merge_left_set = frozenset(merge_left)
        empty_set = frozenset(empty)
        if payload_set & empty_set:
            raise ValueError("payload and empty coordinates must be disjoint")
        if not merge_left_set <= payload_set:
            raise ValueError("merge-left coordinates must contain payload")
        materialized = payload_set | empty_set
        if materialized and materialized != frozenset(
            range(max(materialized) + 1)
        ):
            raise ValueError(
                "leading and internal coordinates must be payload or empty"
            )
        for column in sorted(materialized):
            is_empty = column in empty_set
            code = compose_sparse_code(
                merge_up=(
                    column in previous_empty
                    if is_empty
                    else column in previous_payload
                ),
                merge_left=(
                    not is_empty
                    and column in merge_left_set
                    and column - 1 in payload_set
                ),
                empty=is_empty,
            )
            result.append(
                TopologyEntry(
                    row=row_index,
                    column=column,
                    code=code,
                    empty=is_empty,
                )
            )
        previous_payload = payload_set
        previous_empty = empty_set
    return tuple(result)


def _canonical_columns(values: tuple[int, ...], *, name: str) -> tuple[int, ...]:
    if type(values) is not tuple or any(
        type(value) is not int or value < 0 for value in values
    ):
        raise TypeError(f"{name} columns must be a tuple of non-negative integers")
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{name} columns must be unique and sorted")
    return values


__all__ = (
    "EMPTY",
    "EMPTY_SLOT_CODE",
    "MERGE_BOTH_CODE",
    "MERGE_LEFT_CODE",
    "MERGE_UP_CODE",
    "EmptySlot",
    "ObservedTopologyRow",
    "SpatialSlot",
    "TopologyEntry",
    "TopologySlot",
    "compose_sparse_code",
    "encode_ragged_topology",
    "encode_observed_topology",
    "encode_spatial_topology",
)
