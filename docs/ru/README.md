# Документация IttM

<p align="right">
  <a href="../../README.md"><img alt="Русский" src="https://img.shields.io/badge/%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-%F0%9F%87%B7%F0%9F%87%BA-blue"></a>
  <a href="../en/README.md"><img alt="English" src="https://img.shields.io/badge/English-%F0%9F%87%AC%F0%9F%87%A7-lightgrey"></a>
</p>

[Корневой README](../../README.md) | [English](../en/README.md)

## Карта документации

| Документ | О чём |
| --- | --- |
| [Архитектура проекта](./architecture.md) | Runtime-топология, shared pipeline core и extraction API |
| [Движок и pipeline](./engine/README.md) | Stage recipe, профили, OCR adapters, fallback и таблицы |
| [Markdown-контракт таблиц](./engine/table-markdown-contract.md) | Merge-маркеры и формат табличного результата |
| [Тестирование](./testing.md) | Tiers, regression gates и команды |
| [Debug](./debug.md) | Воспроизводимые OCR-прогоны и артефакты |
| [Sparse OCR, этап 2](./sparse-rewrite-stage2.md) | Параллельные OCR lane, fusion, метрика и debug corpus |
| [Sparse-блоки, этап 5](./sparse-rewrite-stage5.md) | Пересекающиеся контекстные блоки и OR/XOR |
| [Sparse-документ, этап 7](./sparse-rewrite-stage7.md) | Evidence assembly, точная loss-метрика и единый v20 gate |
| [Security](./security.md) | Границы доверия и незакрытые риски |
| [SAST](./sast.md) | Semgrep scope, правила и CI |
| [SBOM](./sbom-report.md) | SCA workflow и зависимости |
| [Ручной запуск Docker](./docker-manual-launch.md) | Запуск без Compose |
| [Границы ответственности](./course/boundaries.md) | Точки входа и владельцы файлов |
| [Критерии курса](./course/course_tasks.md) | Соответствие заданий и реализации |

## Движки

| Движок | Runtime | Передача исходного файла |
| --- | --- | --- |
| Browser Tesseract | Tesseract.js worker | Файл остаётся во вкладке |
| Browser PP-OCRv5 | Offline CTC worker | Файл остаётся во вкладке |
| Local Tesseract | Python OCR backend | Multipart через gateway |
| Local EasyOCR | Python OCR backend | Multipart через gateway |
| External LLM | Provider API | Только после явного consent |

Все OCR adapters возвращают наблюдаемый текст и геометрию. Stage recipe,
fallback и merge принадлежат shared `pipeline-core`.
