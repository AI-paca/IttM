from __future__ import annotations

import re

SUPPORTED_LEXICAL_CORRECTION_MODES = frozenset({"off", "t9_small"})

_CODE_CYRILLIC_TO_LATIN = str.maketrans(
    {
        "А": "A",
        "В": "B",
        "С": "C",
        "Е": "E",
        "Н": "H",
        "К": "K",
        "М": "M",
        "О": "O",
        "Р": "P",
        "Т": "T",
        "Х": "X",
        "У": "Y",
        "І": "I",
        "Ї": "I",
        "Ё": "E",
        "а": "a",
        "в": "b",
        "с": "c",
        "е": "e",
        "н": "h",
        "к": "k",
        "м": "m",
        "о": "o",
        "р": "p",
        "т": "t",
        "х": "x",
        "у": "y",
        "і": "i",
        "ї": "i",
        "ё": "e",
    }
)

_TEXT_LATIN_TO_CYRILLIC = str.maketrans(
    {
        "A": "А",
        "a": "а",
        "B": "В",
        "C": "С",
        "c": "с",
        "E": "Е",
        "e": "е",
        "H": "Н",
        "h": "н",
        "K": "К",
        "k": "к",
        "M": "М",
        "m": "м",
        "O": "О",
        "o": "о",
        "P": "Р",
        "p": "р",
        "T": "Т",
        "X": "Х",
        "x": "х",
        "Y": "У",
        "y": "у",
    }
)


_ACADEMIC_CODE_TOKEN_RE = re.compile(r"(?P<prefix>[A-Za-zА-Яа-яЁёІіЇї0]+)(?P<suffix>(?:[-.]\d+)+)")
_ACADEMIC_CODE_IN_TEXT_RE = re.compile(
    r"(?<![\wА-Яа-яЁёІіЇї])"
    r"(?P<prefix>[OО0oо][PПNНnп][KКkк]|[PП][KКkк]|N[KkКк]|[UУYуy][KКkк])"
    r"(?P<suffix>(?:[-.]\d+)+)"
    r"(?![\wА-Яа-яЁёІіЇї])"
)
_COURT_WORKLOAD_SIDE_COUNT_RE = re.compile(
    r"(?<!\w)"
    r"(?:[，,]\s*)?"
    r"(?:"
    r"KonydecTBo\s+cTopoH\s+BrpanaHckoMAenre"
    r"|[юy]личество\s+сторон\s+в\s+гражданском\s+mene"
    r")"
    r"(?=[,.;:]?(?:\s|$))",
    re.I,
)
_CURRICULUM_INDEX_HINT_RE = re.compile(
    r"(?<![\wА-Яа-яЁёІіЇї])(?:[56]1|Б1|5)\.(?:[0OОoоBВв8])(?:\.\d{1,2})?",
    re.I,
)


def _has_curriculum_index_context(text: str) -> bool:
    if not _has_cyrillic(text):
        return False
    compact = re.sub(r"\s+", "", text.casefold().replace("ё", "е"))
    return (
        "учеб" in compact
        or "дисциплин" in compact
        or "кафедр" in compact
        or "компетенц" in compact
        or "практикум" in compact
        or "информатик" in compact
        or "опк" in compact
        or "пк-" in compact
        or "ук-" in compact
        or "oпk" in compact
        or "пk" in compact
        or "yk" in compact
    )


def _has_cyrillic(text: str) -> bool:
    return any("А" <= char <= "я" or char in "ЁёІіЇї" for char in text)


def _has_latin(text: str) -> bool:
    return any("A" <= char <= "Z" or "a" <= char <= "z" for char in text)


def _normalize_academic_code_prefix(
    prefix: str,
    *,
    allow_plain_latin: bool = False,
) -> str | None:
    has_cyrillic = _has_cyrillic(prefix)
    if not allow_plain_latin and not has_cyrillic:
        return None

    if re.fullmatch(r"[OО0oо][PПNНnп][KКkк]", prefix):
        return "ОПК"
    if re.fullmatch(r"[PП][KКkк]", prefix):
        return "ПК"
    if allow_plain_latin and re.fullmatch(r"N[KkКк]", prefix):
        return "ПК"
    if re.fullmatch(r"[UУYуy][KКkк]", prefix):
        return "УК"
    return None


def _normalize_academic_code_token(token: str, *, allow_plain_latin: bool = False) -> str | None:
    match = _ACADEMIC_CODE_TOKEN_RE.fullmatch(token)
    if not match:
        return None
    prefix = _normalize_academic_code_prefix(
        match.group("prefix"),
        allow_plain_latin=allow_plain_latin,
    )
    if prefix is None:
        return None
    return f"{prefix}{match.group('suffix')}"


def _correct_russian_academic_codes(text: str) -> str:
    matches = list(_ACADEMIC_CODE_IN_TEXT_RE.finditer(text))
    if not matches:
        return text

    has_academic_context = len(matches) >= 2 or _has_cyrillic(text) or bool(re.search(r"(?<!\w)Б\d\.", text))
    if not has_academic_context:
        return text

    def replace(match: re.Match[str]) -> str:
        prefix = _normalize_academic_code_prefix(
            match.group("prefix"),
            allow_plain_latin=True,
        )
        if prefix is None:
            return match.group(0)
        return f"{prefix}{match.group('suffix')}"

    return _ACADEMIC_CODE_IN_TEXT_RE.sub(replace, text)


def _correct_curriculum_indices(text: str) -> str:
    if not _CURRICULUM_INDEX_HINT_RE.search(text):
        return text
    if not _has_curriculum_index_context(text):
        return text

    text = re.sub(
        r"(?<![\wА-Яа-яЁёІіЇї])(?:[56]1|Б1)\.[0OОoо](?=\.?\b)",
        "Б1.О",
        text,
    )
    text = re.sub(
        r"(?<![\wА-Яа-яЁёІіЇї])(?:[56]1|Б1)\.[0OОoо]\.(?=\d{1,2}\b)",
        "Б1.О.",
        text,
    )
    text = re.sub(
        r"(?<![\wА-Яа-яЁёІіЇї])(?:[56]1|Б1)\.[BВв](?=\.?\b)",
        "Б1.В",
        text,
    )
    text = re.sub(
        r"(?<![\wА-Яа-яЁёІіЇї])(?:[56]1|Б1)\.[BВв]\.(?=\d{1,2}\b)",
        "Б1.В.",
        text,
    )
    return re.sub(
        r"(?<![\wА-Яа-яЁёІіЇї])5\.8\.(?=\d{1,2}\b)",
        "Б1.В.",
        text,
    )


def _correct_mixed_cyrillic_token(match: re.Match[str]) -> str:
    token = match.group(0)
    has_cyrillic = _has_cyrillic(token)
    has_latin = _has_latin(token)
    if not has_cyrillic or not has_latin:
        return token
    if any(char.isdigit() for char in token) or "-" in token or "_" in token:
        return token
    return token.translate(_TEXT_LATIN_TO_CYRILLIC)


def _correct_cyrillic_text_confusables(text: str) -> str:
    text = re.sub(r"(?<!\w)Ho(?=\s+[А-Яа-яЁё])", "но", text)
    text = re.sub(
        r"[\wА-Яа-яЁёІіЇї/]+",
        _correct_mixed_cyrillic_token,
        text,
    )
    text = re.sub(r"(?<!\w)[CС][!lІ1]\s*(?=[А-Яа-яЁё])", "CI ", text)
    text = re.sub(r"\bCI\s*запускаетсяи\b", "CI запускается и", text)
    return text


def _correct_identifier_token(match: re.Match[str]) -> str:
    token = match.group(0)
    has_digit = any(char.isdigit() for char in token)
    has_latin = _has_latin(token)
    has_cyrillic = _has_cyrillic(token)
    academic_code = _normalize_academic_code_token(token)
    if academic_code is not None:
        return academic_code
    if not has_cyrillic:
        return token

    separators = [char for char in token if char in "-_/"]
    if not separators and not (has_digit or has_latin):
        return token

    parts = [part for part in re.split(r"[-_/]+", token) if part]
    has_path_separator = "/" in token
    has_code_separator = "_" in token
    has_hyphen_only_separator = bool(separators) and set(separators) == {"-"}

    if has_path_separator:
        has_path_shape = (
            has_digit
            or has_code_separator
            or "." in token
            or any(part.lower().startswith(("test", "src", "app", "ocr")) for part in parts)
            or (parts and bool(re.fullmatch(r"[А-Яа-яЁё]{1,4}", parts[0])) and has_latin)
        )
        if not has_path_shape:
            return token

    if has_hyphen_only_separator:
        has_short_cyrillic_code_prefix = bool(
            parts
            and re.fullmatch(r"[А-Яа-яЁёІіЇї]{1,2}", parts[0])
            and any(re.search(r"[A-Z0-9]", part) for part in parts[1:])
        )
        has_upper_or_digit_code_parts = (
            sum(
                1
                for part in parts
                if any(char.isdigit() for char in part)
                or (
                    any("A" <= char <= "Z" for char in part)
                    and not any("а" <= char <= "я" or char == "ё" for char in part)
                )
            )
            >= 2
        )
        if not (has_digit or has_short_cyrillic_code_prefix or has_upper_or_digit_code_parts):
            return token

    return token.translate(_CODE_CYRILLIC_TO_LATIN)


def _correct_identifier_ocr(text: str) -> str:
    return re.sub(r"[\wА-Яа-яЁёІіЇї]+(?:[-_/][\wА-Яа-яЁёІіЇї]+)+", _correct_identifier_token, text)


def _correct_common_ocr_terms(text: str) -> str:
    replacements = (
        ("РОЕ", "PDF"),
        ("РОР", "PDF"),
        ("РDF", "PDF"),
        ("ОСВ", "OCR"),
        ("ОСК", "OCR"),
        ("ОСЕ", "OCR"),
        ("ОСR", "OCR"),
        ("ОCR", "OCR"),
        ("OСR", "OCR"),
        ("OCE", "OCR"),
        ("0CR", "OCR"),
        ("НТТР", "HTTP"),
        ("ОрепАР!", "OpenAPI"),
        ("АРИСЦ", "API/CLI"),
        ("ЧИАР!", "UI/API"),
        ("Магкаомт", "Markdown"),
        ("Mагкаомт", "Markdown"),
        ("Markdown-KOHTpaKkT", "Markdown-контракт"),
        ("гипите-флаги", "runtime-флаги"),
        ("fallback-cbnaru", "fallback-флаги"),
        ("OCR-cdnaru", "OCR-флаги"),
        ("Гауои-флаги", "Layout-флаги"),
        ("Рефаи!", "Default"),
        ("nopor", "порог"),
        ("попе", "none"),
        ("of f", "off"),
        ("t9_ small", "t9_small"),
        ("t9 small", "t9_small"),
        ("rust+eng", "rus+eng"),
        ("rusteng", "rus+eng"),
        ("Curl-npoBepok", "curl-проверок"),
        ("героп-слое", "report-слое"),
        ("Йад-ключи", "flag-ключи"),
        ("уеййег", "verifier"),
        ("Вазеб4", "Base64"),
        ("Вазе64", "Base64"),
        ("Baseб4", "Base64"),
        ("ArrayBuf Тег", "ArrayBuffer"),
        ("ArrayButter", "ArrayBuffer"),
        ("ar rayBuf fer", "arrayBuffer"),
        ("muttipart", "multipart"),
        ("mutltipart", "multipart"),
        ("conpose", "compose"),
        ("comDose", "compose"),
        (" UD", " up"),
        ("trontend", "frontend"),
        ("fronlend", "frontend"),
        ("провайлеры", "провайдеры"),
        ("npml", "npm"),
        ("Fun Ulnt", "run lint"),
        ("build-Lite", "build-lite"),
        ("APIICLI", "API/CLI"),
        ("APIICLIIJSONlheader", "API/CLI/JSON/header"),
        ("querylheaderIJSONICLI", "query/header/JSON/CLI"),
        ("backendlbrowser", "backend/browser"),
        ("browserldebug", "browser/debug"),
        ("Dockerlbare-metallLite", "Docker/bare-metal/Lite"),
        ("catatog", "catalog"),
        ("p reprocess ing", "preprocessing"),
        ("p reprocess runtime", "preprocess_runtime"),
    )
    for source, target in replacements:
        text = text.replace(source, target)

    text = _COURT_WORKLOAD_SIDE_COUNT_RE.sub(
        "количество сторон в гражданском деле",
        text,
    )
    text = re.sub(r"(?<!\w)С!(?!\w)", "CI", text)
    text = re.sub(r"\b[VУ]8\b", "V8", text)
    text = re.sub(r"\bООМ\b", "OOM", text)
    text = re.sub(r"\bСРУ\b", "CPU", text)
    text = re.sub(r"\bСPU\b", "CPU", text)
    text = re.sub(r"\bRAM\b", "RAM", text)
    text = re.sub(r"\bCl(?=\s+verifier)", "CI", text)
    return text


_KNOWN_SNAKE_IDENTIFIERS = (
    "engine_type",
    "pipeline_profile",
    "pdf_mode",
    "pipeline_flags",
    "overrides_enabled",
    "preprocess_runtime",
    "ocr_runtime",
    "ocr_languages",
    "ocr_max_dimension",
    "ocr_max_image_pixels",
    "browser_cache_worker",
    "browser_profile_reason",
    "pdf_render_scale",
    "layout_selector",
    "layout_stage",
    "layout_decision",
    "layout_runtime_stage",
    "table_word_formatter",
    "table_word_formatters",
    "table_word_recognition",
    "table_layout_normalization",
    "profile_flag_items",
    "profile_flags",
    "pipeline_flag_catalog",
    "pipeline_flags_payload",
    "resolve_pipeline_profile",
    "normalize_pdf_mode",
    "extract_pdf_text_layer_pages",
    "pdf_text_layer_min_chars",
    "pdf_text_layer_min_words",
    "pdf_text_layer_min_page_ratio",
    "backend_auto_standard",
    "backend_tesseract_standard",
    "backend_easyocr_standard",
    "backend_easyocr_table",
    "backend_easyocr_spatial",
    "backend_curriculum",
    "backend_plain_text",
    "table_raw_text_fallback",
    "table_raw_text_fallback_psm",
    "table_raw_text_fallback_min_rows",
    "table_raw_text_fallback_min_cols",
    "table_raw_text_fallback_max_cols",
    "table_raw_text_fallback_min_ratio",
    "sparse_text_fallback_engine",
    "sparse_text_fallback_min_tokens",
    "sparse_text_fallback_min_ratio",
    "dense_grid_fallback",
    "dense_grid_target_width",
    "ocr_border_pixels",
    "edge_word_fallback_psm",
    "edge_word_fallback_min_tokens",
    "ocr_language_priority",
    "ocr_text_region_psm",
    "ocr_document_region_psm",
    "ocr_wide_text_region_psm",
    "ocr_table_word_psm",
    "ocr_large_table_word_psm",
    "text_region_psm",
    "document_region_psm",
    "wide_text_region_psm",
    "table_word_psm",
    "large_table_word_psm",
    "lexical_correction",
    "ocr_language_retry",
    "structural_output",
    "image_preprocessing",
    "feature_extractors",
    "allowed_stages",
    "default_parameters",
    "max_region_height",
    "min_region_height",
    "min_separator_coverage",
    "direct_region_ocr",
    "medium_page_segmentation",
    "grid_min_confirmed_cell_ratio",
    "table_min_word_cell_coverage",
    "wide_table_min_word_cell_coverage",
    "table_min_cell_coverage",
    "max_table_cell_ocr_calls",
)


def _identifier_phrase_pattern(identifier: str) -> str:
    return r"(?<![\w])" + r"[\s_]+".join(re.escape(part) for part in identifier.split("_")) + r"(?![\w])"


def _correct_doc_identifier_phrases(text: str) -> str:
    text = re.sub(
        r"\b[O0О]CR[\s_]+PIPELINE[\s_]+PRO[\s_]*FILES\b",
        "OCR_PIPELINE_PROFILES",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bDEFAULT[\s_]+ENGINE[\s_]+PIPELINE[\s_]+PRO[\s_]*FILES\b",
        "DEFAULT_ENGINE_PIPELINE_PROFILES",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bPIPELINE[\s_]+PRO[\s_]*FILES\b",
        "DEFAULT_ENGINE_PIPELINE_PROFILES",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bpipeline[\s_]+pipeline[\s_]+_?flags[\s_]*payload\b",
        "pipeline_flag_catalog, pipeline_flags_payload",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bpipeline[\s_]+flag[\s_]*_?catalog\b",
        "pipeline_flag_catalog",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b_extract[\s_]+pdf[\s_]+text[\s_]+layer[\s_]+pag[\s_]*es\b",
        "_extract_pdf_text_layer_pages",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bPDF[\s_]+TEXT[\s_]+(?:ГАУЕВ|LAYER)[\s_]+MIN[\s_]+(?:РАбЕ|PAGE)[\s_]+(?:ВАТТО|RATIO)\b",
        "PDF_TEXT_LAYER_MIN_PAGE_RATIO",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bOcr[\s_]*Pipeline[\s_]*Profile\b",
        "OcrPipelineProfile",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bLayout[\s_]*Pipeline[\s_]*Config\b",
        "LayoutPipelineConfig",
        text,
        flags=re.I,
    )
    for identifier in _KNOWN_SNAKE_IDENTIFIERS:
        text = re.sub(
            _identifier_phrase_pattern(identifier),
            identifier,
            text,
            flags=re.I,
        )
    text = re.sub(r"\b([A-Za-z0-9_]+)\.\s+(py|ts|tsx|js|md|sh|json)\b", r"\1.\2", text)
    text = re.sub(r"\b([A-Za-z0-9_]+)\.\s*ру\b", r"\1.py", text)
    text = re.sub(r"\b([A-Za-z0-9_]+)\s+ру\b", r"\1.py", text)
    text = re.sub(r"\b([A-Za-z0-9_]+)md\b", r"\1.md", text)
    text = re.sub(r"\bpipeline[\s_]+config\.py\b", "pipeline_config.py", text, flags=re.I)
    text = re.sub(r"\bpipeline[\s_]+flags\.py\b", "pipeline_flags.py", text, flags=re.I)
    text = re.sub(r"\bprofile[\s_]+name\b", "profile_name", text, flags=re.I)
    text = re.sub(r"\bengine[\s_]+type\b", "engine_type", text, flags=re.I)
    text = re.sub(r"\bpdf[\s_]+mode\b", "pdf_mode", text, flags=re.I)
    text = re.sub(r"\blexical[\s_]+correction\b", "lexical_correction", text, flags=re.I)
    text = re.sub(r"\boff[\s/]+t9_small\b", "off / t9_small", text, flags=re.I)
    text = re.sub(r"\blarge[\s_]+tabte[\s_]+word[\s_]+р[тm]\b", "large_table_word_psm", text, flags=re.I)
    text = re.sub(r"\btabtLe[\s_]+мог4[\s_]+recognition\b", "table_word_recognition", text, flags=re.I)
    text = re.sub(
        r"\bedge[\s_]+word[\s_]+fallback[\s_]+toke[\s_]*ns\b", "edge_word_fallback_min_tokens", text, flags=re.I
    )
    text = re.sub(
        r"\bейде[\s_]+word[\s_]+fallback[\s_]+toke[\s_]*ns\b", "edge_word_fallback_min_tokens", text, flags=re.I
    )
    text = re.sub(
        r"\bsparse[\s_]+text_fallback[\s_]+min[\s_]+to[\s_]*kens\b", "sparse_text_fallback_min_tokens", text, flags=re.I
    )
    text = re.sub(
        r"\bsparse[\s_]*_?text_fallback[\s_]+тїп[\s_]+га[\s_]+tio\b", "sparse_text_fallback_min_ratio", text, flags=re.I
    )
    text = re.sub(
        r"\b(ocr|web|docs|scripts|debug)\s*/\s*([^\s,;:|)]+)",
        _collapse_known_root_slash_spacing,
        text,
    )
    text = re.sub(r"\b(ocr/app|web/src|docs/ru)\s*/\s*", r"\1/", text)
    text = re.sub(r"\b[Il1]v[Il1](?=/pipeline/flags)", "/v1", text)
    text = re.sub(r"\bvl(?=/pipeline/flags)", "/v1", text)
    text = re.sub(r"/\s+([A-Za-z0-9_./-]+)", _collapse_pathish_slash_spacing, text)
    text = re.sub(r"\brus\+eng\b", "rus+eng", text, flags=re.I)
    text = re.sub(r"\bchi\s+sim\b", "chi_sim", text, flags=re.I)
    return text


def _collapse_pathish_slash_spacing(match: re.Match[str]) -> str:
    text = match.string
    before = text[: match.start()].rstrip()
    left = before.rsplit(maxsplit=1)[-1] if before else ""
    right = match.group(1)
    left_clean = left.strip("`'\"(").lower()
    right_clean = right.strip("`'\",).").lower()
    pathish_left = {
        "text",
        "application",
        "api",
        "v1",
        "ocr",
        "web",
        "docs",
        "scripts",
        "debug",
        "gateway",
        "src",
        "tests",
        "reference",
        "fixtures",
        "convert",
        "extract",
        "tasks",
    }
    pathish_right_prefixes = (
        "api",
        "app",
        "capabilities",
        "convert",
        "diagnostics",
        "event",
        "events",
        "extract",
        "flags",
        "health",
        "json",
        "markdown",
        "ndjson",
        "pipeline",
        "plain",
        "services",
        "src",
        "stream",
        "tasks",
        "text",
        "v1",
        "x-",
    )
    if (
        "." in right
        or "_" in left
        or "_" in right
        or left_clean in pathish_left
        or right_clean.startswith(pathish_right_prefixes)
    ):
        return "/" + right
    return match.group(0)


def _collapse_known_root_slash_spacing(match: re.Match[str]) -> str:
    root = match.group(1)
    right = match.group(2)
    right_clean = right.strip("`'\",).").lower()
    if (
        "." in right
        or "_" in right
        or right_clean.startswith(
            (
                "api",
                "app",
                "fixtures",
                "reference",
                "services",
                "src",
                "tests",
            )
        )
    ):
        return f"{root}/{right}"
    return match.group(0)


def _normalize_cjk_spacing(text: str) -> str:
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)


_RESOLUTION_CONTEXT_RE = re.compile(
    r"\b(?:display|screen|monitor|resolution|pixel|pixels|ips|oled|lcd|retina|inch|laptop|notebook|fhd)\b",
    re.I,
)


def _split_compact_resolution_numbers(text: str) -> str:
    if not _RESOLUTION_CONTEXT_RE.search(text):
        return text

    def replace(match: re.Match[str]) -> str:
        width = int(match.group(1))
        height = int(match.group(2))
        if 640 <= width <= 9999 and 480 <= height <= 9999:
            return f"{width}x{height}"
        return match.group(0)

    return re.sub(
        r"(?<![\w])(\d{3,4})(\d{3,4})(?![\w])",
        replace,
        text,
    )


def _correct_commerce_ocr_confusables(text: str, *, prefer_euro: bool) -> str:
    value = text
    if prefer_euro:
        value = re.sub(r"£(?=\s*\d)", "€", value)
    value = _split_compact_resolution_numbers(value)
    value = re.sub(r"\bFHO\b", "FHD", value)
    value = re.sub(r"\bDRS\b", "DDR5", value)
    value = re.sub(r"\bRAM\s+868\b", "RAM 8GB", value)
    value = re.sub(r"\b868\s+RAM\b", "8GB RAM", value)
    return value


def _correct_ui_ocr_line(line: str, *, prefer_euro: bool = False) -> str:
    value = line.strip()
    if not value:
        return line

    prefixes = (
        "[1 ",
        "{1 ",
        "11 ",
        "8 + ",
        "{+ ",
        "8 * ",
        "> ",
        "{= ",
        "=> ",
        "和 ",
        "© ",
    )
    for prefix in prefixes:
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break

    if value.endswith(" ©"):
        value = value[:-2].rstrip()
    if value.endswith(" -¥.") or value.endswith(" -¥"):
        value = value.rsplit(" -¥", 1)[0].rstrip()
    value = value.replace(" jun ", " Jun ")
    value = re.sub(
        r"(\d+)\s+[?2]\s+(скидки\s+на\s+заказ)",
        r"\1 ₽ \2",
        value,
        flags=re.I,
    )
    value = re.sub(r"(от\s+\d+)\s+[?2]\b", r"\1 ₽", value, flags=re.I)
    if re.fullmatch(r"[a-z0-9]{8,}\s+@", value, re.I):
        value = value[:-2].rstrip()
    value = _correct_common_ocr_terms(value)
    value = _correct_doc_identifier_phrases(value)
    value = _normalize_cjk_spacing(value)
    value = _correct_commerce_ocr_confusables(value, prefer_euro=prefer_euro)
    value = _correct_cyrillic_text_confusables(value)
    value = _correct_identifier_ocr(value)
    value = _correct_russian_academic_codes(value)
    value = _correct_curriculum_indices(value)
    return value


def apply_lexical_correction(text: str, mode: str) -> str:
    if mode not in SUPPORTED_LEXICAL_CORRECTION_MODES:
        known = ", ".join(sorted(SUPPORTED_LEXICAL_CORRECTION_MODES))
        raise ValueError(f"Unknown lexical correction mode '{mode}'. Known modes: {known}")
    if mode == "off":
        return text

    prefer_euro = "€" in text
    return "\n".join(_correct_ui_ocr_line(line, prefer_euro=prefer_euro) for line in text.splitlines())
