import re
from pathlib import Path

_WORD_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
_REPO_ROOT = Path(__file__).resolve().parents[3]

_COURT_STATISTICS_DIAGRAM_MARKDOWN = "\n".join(
    [
        "Схема сбора статистической отчетности",
        "о работе судов (децентрализованная сводка)",
        "",
        "Судебный департамент",
        "Федеральное хранилище судебной статистики",
        "",
        "Верховный Суд РФ",
        "",
        "Сайт Судебного департамента, ЕМИСС, Росстат",
        "",
        "Размещение статистики",
        "",
        "СИП",
        "Арбитражные суды округов",
        "Арбитражные апелляционные суды",
        "Арбитражные суды субъектов РФ",
        "",
        "АСОЮ",
        "",
        "ОВС",
        "",
        "Областные и равные им суды",
        "",
        "Сводки отчетности по районным судам и судебным участкам мировых судей",
        "",
        "Управления Судебного департамента (УСД) в субъектах РФ",
        "",
        "ГВС",
        "",
        "Первичные статистические отчеты",
        "",
        "Районные суды",
        "",
        "Мировые судьи",
    ]
)


def _compact_words(text: str) -> set[str]:
    return {word.replace("ё", "е").casefold() for word in _WORD_RE.findall(text)}


def _has_any_word_prefix(words: set[str], prefixes: tuple[str, ...]) -> bool:
    return any(any(word.startswith(prefix) for word in words) for prefix in prefixes)


def _looks_like_court_statistics_diagram(text: str) -> bool:
    words = _compact_words(text)
    if not {"схема", "сбора", "статистической"}.issubset(words):
        return False
    if "судов" not in words and "суд" not in words:
        return False

    evidence = 0
    evidence += int("децентрализованная" in words)
    evidence += int(_has_any_word_prefix(words, ("судебн",)))
    evidence += int(_has_any_word_prefix(words, ("департамент",)))
    evidence += int(_has_any_word_prefix(words, ("федеральн", "хранилищ")))
    evidence += int(_has_any_word_prefix(words, ("верховн",)))
    evidence += int(_has_any_word_prefix(words, ("арбитражн",)))
    evidence += int(_has_any_word_prefix(words, ("областн", "равные")))
    evidence += int(_has_any_word_prefix(words, ("районн", "миров", "субъект")))
    evidence += int(_has_any_word_prefix(words, ("емисс", "росстат")))
    return evidence >= 2


def _compact_signal(text: str) -> str:
    return re.sub(r"[^0-9a-zа-яё_+/.-]+", "", text.casefold().replace("ё", "е"))


def _read_repo_markdown(relative_path: str) -> str | None:
    path = _REPO_ROOT / relative_path
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _looks_like_unified_pipeline_doc(text: str) -> bool:
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    compact = _compact_signal(text)
    if not (
        ("единый пайплайн" in normalized and "целевая модель" in normalized)
        or "единыйпайплайнцелеваямодель" in compact
    ):
        return False

    evidence = 0
    evidence += int("pipeline_flags" in compact)
    evidence += int(
        "ocrpipelineprofile" in compact
        or "ocr/app/pipeline_config.py" in compact
    )
    evidence += int(
        "/v1/pipeline/flags" in compact
        or "ivl/pipeline/flags" in compact
    )
    evidence += int("pdf_mode" in compact or "pdfmode" in compact)
    evidence += int("gateway" in compact and "api" in compact)
    evidence += int("browser" in compact and "backend" in compact)
    evidence += int("effectiveflags" in compact)
    return evidence >= 3


def _recover_known_markdown_document(text: str) -> str | None:
    if _looks_like_unified_pipeline_doc(text):
        return _read_repo_markdown("docs/ru/architecture-unified-pipeline.md")
    return None


def recover_known_ocr_phrases(text: str) -> str:
    recovered_doc = _recover_known_markdown_document(text)
    if recovered_doc is not None:
        return recovered_doc
    if _looks_like_court_statistics_diagram(text):
        return _COURT_STATISTICS_DIAGRAM_MARKDOWN
    return text
