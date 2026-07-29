from __future__ import annotations

import re

_SCRIPT_PATTERNS = {
    "latin": re.compile(r"[A-Za-z]"),
    "cyrillic": re.compile(r"[\u0400-\u04ff]"),
    "cjk": re.compile(r"[\u3400-\u9fff]"),
    "greek": re.compile(r"[\u0370-\u03ff]"),
}

_LATIN_TECH_CONTEXT = re.compile(
    r"\b(?:laptop|notebook|display|windows|ram|ssd|intel|amd|ryzen|celeron|"
    r"core|wifi|hdmi|asus|lenovo|acer|dell|vivobook|ideapad|thinkpad|"
    r"microsoft|prime|price|delivery|api|backend|browser|config|easyocr|"
    r"engine|frontend|ocr|pipeline|profile|tesseract)\b",
    re.I,
)
_CODE_PATH_TOKEN_RE = re.compile(
    r"[\w\u0400-\u04ff]+(?:[./\\_-][\w\u0400-\u04ff]+)+",
    re.UNICODE,
)
_CODE_FILE_EXTENSIONS = frozenset(
    {
        "css",
        "html",
        "js",
        "json",
        "jsx",
        "md",
        "py",
        "rs",
        "sh",
        "ts",
        "tsx",
        "txt",
        "wasm",
        "yml",
        "yaml",
    }
)
_CODE_ROOTS = frozenset(
    {
        "app",
        "backend",
        "debug",
        "docs",
        "ocr",
        "scripts",
        "src",
        "tests",
        "web",
    }
)
_COMMON_CYRILLIC_WORDS = frozenset(
    {
        "а",
        "без",
        "в",
        "для",
        "если",
        "и",
        "или",
        "к",
        "как",
        "на",
        "не",
        "но",
        "от",
        "по",
        "с",
        "то",
        "у",
        "что",
    }
)


def _script_counts(text: str) -> dict[str, int]:
    return {script: len(pattern.findall(text)) for script, pattern in _SCRIPT_PATTERNS.items()}


def _script_tokens(text: str, scripts: tuple[str, ...]) -> list[str]:
    pattern = "|".join(_SCRIPT_PATTERNS[script].pattern for script in scripts)
    return re.findall(f"(?:{pattern})+", text)


def looks_like_mixed_script_ocr_noise(text: str) -> bool:
    counts = _script_counts(text)
    alpha_count = sum(counts.values())
    if alpha_count < 12:
        return False

    dominant_script, dominant_count = max(counts.items(), key=lambda item: item[1])
    secondary_count = alpha_count - dominant_count
    if secondary_count < 2 or dominant_count / alpha_count < 0.68:
        return False

    secondary_ratio = secondary_count / alpha_count
    if secondary_ratio > 0.32:
        return False

    if dominant_script == "latin":
        foreign_tokens = _script_tokens(text, ("cyrillic", "cjk", "greek"))
        cyrillic_tokens = _script_tokens(text, ("cyrillic",))
        meaningful_cyrillic_tokens = [token for token in cyrillic_tokens if len(token) >= 4]
        if len(cyrillic_tokens) >= 3 and len(meaningful_cyrillic_tokens) >= 2:
            return False
        ascii_words = re.findall(r"\b[A-Za-z]{2,}\b", text)
        short_foreign_ratio = sum(len(token) <= 4 for token in foreign_tokens) / max(1, len(foreign_tokens))
        return (
            len(ascii_words) >= 4
            and bool(_LATIN_TECH_CONTEXT.search(text))
            and (short_foreign_ratio >= 0.5 or secondary_count <= 10)
        )

    if dominant_script == "cyrillic":
        cyrillic_words = [
            word
            for word in re.findall(r"[\w\u0400-\u04ff]+", text.lower(), re.UNICODE)
            if re.search(r"[\u0400-\u04ff]", word)
        ]
        # A real Cyrillic sentence commonly embeds a short foreign term,
        # abbreviation, or parenthetical title.  Script agreement alone is
        # not evidence that the surrounding language became less plausible.
        # Confusable product/code noise does not normally contain multiple
        # ordinary Cyrillic function words.
        if sum(word in _COMMON_CYRILLIC_WORDS for word in cyrillic_words) >= 2:
            return False
        latin_tokens = _script_tokens(text, ("latin",))
        short_latin_ratio = sum(len(token) <= 3 for token in latin_tokens) / max(1, len(latin_tokens))
        return len(latin_tokens) >= 2 and short_latin_ratio >= 0.7

    foreign_tokens = _script_tokens(text, tuple(script for script in _SCRIPT_PATTERNS if script != dominant_script))
    short_foreign_ratio = sum(len(token) <= 3 for token in foreign_tokens) / max(1, len(foreign_tokens))
    return short_foreign_ratio >= 0.75


def looks_like_cyrillic_tech_ocr_noise(text: str) -> bool:
    counts = _script_counts(text)
    cyrillic = counts.get("cyrillic", 0)
    if cyrillic < 10 or counts.get("latin", 0) > max(2, round(cyrillic * 0.20)):
        return False

    words = re.findall(r"[\w\u0400-\u04ff]+", text.lower(), re.UNICODE)
    if not words:
        return False
    common_words = sum(word in _COMMON_CYRILLIC_WORDS for word in words)
    if common_words >= 2:
        return False

    cyrillic_words = [word for word in words if re.search(r"[\u0400-\u04ff]", word)]
    embedded_digit_words = [
        word
        for word in cyrillic_words
        if any(character.isdigit() for character in word)
    ]
    tech_punctuation = sum(character in "/_{}[]?\\|" for character in text)
    if embedded_digit_words and tech_punctuation >= 1:
        return True
    if tech_punctuation >= 3 and len(cyrillic_words) >= 3:
        return True
    return False


def code_path_quality_score(text: str) -> float:
    """Score code-like OCR candidates before lexical correction.

    Mixed OCR often turns Latin paths into Cyrillic lookalikes
    (``ocr/app/pipeline_config.py`` -> ``осг/арр/...``).  This signal is kept
    generic: it rewards stable path/snake_case/file-extension shapes and
    penalizes script-confused path tokens, without naming fixture content.
    """
    tokens = _CODE_PATH_TOKEN_RE.findall(text)
    if not tokens:
        return 0.0

    score = 0.0
    total_non_space = max(1, sum(not character.isspace() for character in text))
    for token in tokens:
        token_score = _code_path_token_score(token)
        if not token_score:
            continue
        token_density = min(1.0, len(token) / total_non_space)
        score += token_score * (0.35 + (0.65 * token_density))
    return max(-24.0, min(24.0, score))


def _code_path_token_score(token: str) -> float:
    latin = sum(bool(re.match(r"[A-Za-z]", character)) for character in token)
    cyrillic = sum(bool(re.match(r"[\u0400-\u04ff]", character)) for character in token)
    digits = sum(character.isdigit() for character in token)
    separators = sum(character in "._-/\\" for character in token)
    if separators == 0:
        return 0.0

    lowered = token.lower().strip("`'\"()[]{}.,:;!?|")
    parts = [part for part in re.split(r"[./\\_-]+", lowered) if part]
    extension = lowered.rsplit(".", 1)[-1] if "." in lowered else ""
    has_code_extension = extension in _CODE_FILE_EXTENSIONS
    has_code_root = bool(parts and parts[0] in _CODE_ROOTS)
    has_snake_identifier = "_" in lowered and latin >= 3
    has_path_separator = "/" in lowered or "\\" in lowered
    has_mixed_code_marker = (
        (latin > 0 or digits > 0 or "_" in lowered or has_code_extension)
        and cyrillic > 0
        and (
            has_code_extension
            or has_code_root
            or "_" in lowered
            or "/" in lowered
            or "\\" in lowered
        )
    )
    if not (
        has_code_extension
        or has_code_root
        or has_snake_identifier
        or (has_path_separator and (latin >= 2 or "_" in lowered))
        or has_mixed_code_marker
    ):
        return 0.0

    clean = latin + digits + separators
    token_len = max(1, len(token))
    clean_ratio = clean / token_len
    score = 0.0
    if has_path_separator:
        score += 4.0
    if has_code_root:
        score += 5.0
    if has_code_extension:
        score += 6.0
    if has_snake_identifier:
        score += 4.0
    if len(parts) >= 3:
        score += 2.0
    score += clean_ratio * 5.0

    if cyrillic:
        score -= min(18.0, 2.2 * cyrillic)
        if has_mixed_code_marker:
            score -= 4.0
    odd = sum(
        not character.isalnum() and character not in "._-/\\"
        for character in token
    )
    score -= min(8.0, odd * 2.0)
    return score


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
        weird = sum(not character.isalnum() and character not in "._:/-+%()[]№₽$€" for character in token)
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
        if not character.isspace() and not character.isalnum() and character not in "._:/-+%()[]№₽$€=<>^{}*,;:!?|"
    )
    non_space_chars = max(1, sum(not character.isspace() for character in text))
    useful_ratio = useful_chars / non_space_chars
    noisy_ratio = (noisy_chars + punctuation_noise) / non_space_chars
    avg_token_score = score / max(1, len(tokens))
    length_bonus = min(24.0, len(tokens) * 1.4)
    raw_noise_penalty = max(0, len(raw_tokens) - len(tokens)) * 0.4
    return (avg_token_score * 6.0) + length_bonus + (useful_ratio * 24.0) - (noisy_ratio * 60.0) - raw_noise_penalty


def text_noise_ratio(text: str) -> float:
    non_space = [character for character in text if not character.isspace()]
    if not non_space:
        return 1.0
    noisy = [
        character
        for character in non_space
        if not character.isalnum() and character not in "._:/-+%()[]№₽$€=<>^{}*,;:!?|"
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
    if looks_like_mixed_script_ocr_noise(text):
        return True
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
                if existing_bbox and len(existing_bbox) == 4 and bbox_overlap_ratio(bbox, existing_bbox) >= 0.72:
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
