# IttM documentation

- [Русская документация](./ru/README.md)
- [English documentation](./en/README.md)

Документация описывает только действующие интерфейсы, ограничения и процедуры
сопровождения. [Текущее развитие](./ru/roadmap/development-branches.md)
отделено от [истории по commit anchors](./ru/roadmap/history.md) и
[дальнего видения](./ru/roadmap/vision.md). Полный разбор одного настоящего
pipeline run находится в [`debug/EXAMPLE.md`](../debug/EXAMPLE.md).

Архитектура, OCR pipeline и SAST редактируются в Draw.io и экспортируются в
SVG/PNG; roadmap поддерживается напрямую в SVG. Все файлы находятся в
[`assets`](./assets/). Инструкции, позволяющие AI-агентам не читать целиком
большие scanner-конфигурации и отчёты:
[SAST](../.semgrep/AGENTS.md) и [SCA](../.sca/AGENTS.md).
