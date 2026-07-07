from __future__ import annotations

import re


def text_quality_score(text: str) -> float:
    raw_tokens = [token for token in re.split(r"\s+", text.strip()) if token]
    tokens = re.findall(r"[\w]+(?:[.+:/-][\w]+)*", text, re.UNICODE)
    if not tokens:
        return 0.0

    score = 0.0
    useful_chars = 0
    noisy_chars = 0
    for token in tokens:
        letters = sum(character.isalpha() for character in token)
        digits = sum(character.isdigit() for character in token)
        separators = sum(character in "._:/-+" for character in token)
        weird = sum(
            not character.isalnum()
            and character not in "._:/-+%()[]№₽$€"
            for character in token
        )
        script_count = sum(
            bool(re.search(pattern, token))
            for pattern in (
                r"[A-Za-z]",
                r"[А-Яа-яЁё]",
                r"[\u4e00-\u9fff]",
                r"[Α-ω]",
            )
        )
        useful_chars += letters + digits
        noisy_chars += weird
        token_score = min(10.0, max(1, len(token)))
        if letters or digits:
            token_score += 2.0
        if len(token) >= 3:
            token_score += 1.0
        if script_count > 1 and not (digits or separators):
            token_score -= 2.0
        token_score -= weird * 2.5
        token_score -= max(0, len(re.findall(r"(.)\1{3,}", token))) * 2.0
        score += max(0.0, token_score)

    punctuation_noise = sum(
        1
        for character in text
        if not character.isspace()
        and not character.isalnum()
        and character not in "._:/-+%()[]№₽$€=<>^{}*,;:!?|"
    )
    non_space_chars = max(1, sum(not character.isspace() for character in text))
    useful_ratio = useful_chars / non_space_chars
    noisy_ratio = (noisy_chars + punctuation_noise) / non_space_chars
    avg_token_score = score / max(1, len(tokens))
    length_bonus = min(24.0, len(tokens) * 1.4)
    raw_noise_penalty = max(0, len(raw_tokens) - len(tokens)) * 0.4
    return (
        (avg_token_score * 6.0)
        + length_bonus
        + (useful_ratio * 24.0)
        - (noisy_ratio * 60.0)
        - raw_noise_penalty
    )


def text_noise_ratio(text: str) -> float:
    non_space = [character for character in text if not character.isspace()]
    if not non_space:
        return 1.0
    noisy = [
        character
        for character in non_space
        if not character.isalnum()
        and character not in "._:/-+%()[]№₽$€=<>^{}*,;:!?|"
    ]
    return len(noisy) / len(non_space)


def words_to_text(words: list[dict]) -> str:
    return " ".join(str(word.get("text", "")) for word in words)


def word_score(word: dict) -> float:
    text = str(word.get("text", ""))
    try:
        conf = float(word.get("conf"))
    except (TypeError, ValueError):
        conf = -1.0
    return text_quality_score(text) + max(0.0, conf) / 35.0


def word_candidate_quality(words: list[dict]) -> float:
    if not words:
        return 0.0
    scores = [word_score(word) for word in words]
    return sum(scores) / len(scores)


def should_retry_word_languages(words: list[dict]) -> bool:
    if len(words) < 3:
        return True
    text = words_to_text(words)
    if text_noise_ratio(text) >= 0.18:
        return True
    return word_candidate_quality(words) < 5.5


def bbox_overlap_ratio(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    first_area = max(1, (first[2] - first[0]) * (first[3] - first[1]))
    second_area = max(1, (second[2] - second[0]) * (second[3] - second[1]))
    return intersection / min(first_area, second_area)


def merge_language_word_candidates(
    candidates: list[list[dict]],
) -> list[dict]:
    if not candidates:
        return []

    merged: list[dict] = list(candidates[0])
    for words in candidates[1:]:
        for word in words:
            bbox = word.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            overlaps_primary = False
            for existing in merged:
                existing_bbox = existing.get("bbox")
                if (
                    existing_bbox
                    and len(existing_bbox) == 4
                    and bbox_overlap_ratio(bbox, existing_bbox) >= 0.72
                ):
                    overlaps_primary = True
                    break
            if not overlaps_primary:
                merged.append(word)
    return sorted(
        merged,
        key=lambda word: (
            (word["bbox"][1] + word["bbox"][3]) / 2,
            word["bbox"][0],
        ),
    )
