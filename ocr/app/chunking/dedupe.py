from typing import List


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def dedupe_chunks(chunks: List[str]) -> List[str]:
    result = []
    seen = set()

    for chunk in chunks:
        normalized = _normalize(chunk)
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(chunk)

    return result
