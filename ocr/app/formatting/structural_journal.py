from __future__ import annotations

import json
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from typing import BinaryIO, Protocol

STRUCTURAL_JOURNAL_VERSION = 1


@dataclass(frozen=True)
class JournalRef:
    offset: int
    length: int


class StructuralJournal(Protocol):
    def append(self, parts: Iterable[str]) -> JournalRef:
        ...

    def parts(self, reference: JournalRef) -> Iterable[str]:
        ...


def encode_structural_record(
    *,
    kind: str,
    parts: Iterable[str],
    anchor: tuple[int, int] = (0, 0),
    codes: tuple[tuple[int, int, int], ...] = (),
    list_marker: bool = False,
    content_left: int | None = None,
    flags: tuple[str, ...] = (),
) -> str:
    return json.dumps(
        {
            "v": STRUCTURAL_JOURNAL_VERSION,
            "kind": kind,
            "anchor": anchor,
            "codes": codes,
            "list_marker": list_marker,
            "content_left": content_left,
            "flags": flags,
            "parts": [str(part) for part in parts],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


class TemporaryStructuralJournal:
    def __init__(self) -> None:
        self._stream: BinaryIO | None = None

    def __enter__(self) -> TemporaryStructuralJournal:
        self._stream = tempfile.TemporaryFile(mode="w+b")
        return self

    def __exit__(self, *_exc_info) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def append(self, parts: Iterable[str]) -> JournalRef:
        stream = self._require_stream()
        payload = (
            json.dumps(
                {
                    "v": STRUCTURAL_JOURNAL_VERSION,
                    "parts": [str(part) for part in parts],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        reference = JournalRef(
            offset=stream.tell(),
            length=len(payload),
        )
        stream.write(payload)
        return reference

    def parts(self, reference: JournalRef) -> Iterable[str]:
        stream = self._require_stream()
        stream.seek(reference.offset)
        payload = json.loads(
            stream.read(reference.length).decode("utf-8"),
        )
        if payload.get("v") != STRUCTURAL_JOURNAL_VERSION:
            raise ValueError("Unsupported structural journal record")
        return tuple(str(part) for part in payload.get("parts", ()))

    def _require_stream(self) -> BinaryIO:
        if self._stream is None:
            raise RuntimeError("Structural journal is not open")
        return self._stream
