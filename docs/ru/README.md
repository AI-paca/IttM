# Документация IttM

[Корневой README](../../README.md) | [English](../en/README.md)

Документация предназначена для сопровождения действующего сервиса:

| Документ                                             | Когда нужен                                         |
| ---------------------------------------------------- | --------------------------------------------------- |
| [Сопровождение pipeline](./pipeline/README.md)       | Проверить сервис, прочитать запрос и результат      |
| [Архитектура](./architecture.md)                     | Найти компонент и публичный интерфейс               |
| [Этапы pipeline](./architecture-unified-pipeline.md) | Раскрыть pipeline black box и найти runtime handler |
| [Ограничения](./architecture-limitations.md)         | Проверить upload, память, PDF и очередь             |
| [Полный пример](../../debug/EXAMPLE.md)              | Проследить matrix, objects, blocks и результат      |
| [Безопасность](./security.md)                        | Проверить trust boundary, SAST и SCA                |
| [Развитие](./roadmap/development-branches.md)        | Увидеть текущую работу и критерии готовности        |
| [История по commits](./roadmap/history.md)           | Проверяемая историческая ось проекта                |
| [Видение](./roadmap/vision.md)                       | Будущие и далёкие направления                       |
| [Тестирование](./testing.md)                         | Выбрать тест по его ответственности                 |
| [SAST](./sast.md)                                    | Разобрать first-party security finding              |
| [SCA/SBOM](./sbom-report.md)                         | Разобрать dependency/image finding                  |

Технические справочники:

- [Task API](../en/task-api.md);
- [backend pipeline](../en/backend-pipeline.md);
- [opt-in sparse pipeline](../en/sparse-pipeline.md);
- [runtime scripts](../en/runtime-scripts.md);
- [debug scripts](../en/scripts.md);
- [локальные OCR/VLM deployments](../en/ollama-deploy.md);
- [полная English-карта тестов](../en/testing.md);
- [`pipeline-core/README.md`](../../pipeline-core/README.md) — Rust ABI.

Архитектура, pipeline и SAST имеют редактируемые Draw.io-исходники и
экспортированные SVG/PNG в [`docs/assets`](../assets/). Roadmap редактируется
непосредственно как SVG.

Инструкции для AI-агентов:

- [`.semgrep/AGENTS.md`](../../.semgrep/AGENTS.md) — какой тест отвечает за
  SAST finding и какую часть ruleset читать;
- [`.sca/AGENTS.md`](../../.sca/AGENTS.md) — как разбирать SCA без полного
  чтения больших JSON/SBOM и когда допустимо менять accepted risk.
