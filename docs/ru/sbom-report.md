# SBOM и зависимости

[English](../en/sbom-report.md) | [Документация](./README.md)

Автоматически публикуемый SBOM пока не сформирован.

Текущие источники зависимостей:

- `package-lock.json` для web/gateway/edge;
- `ocr/requirements-light.txt` для runtime OCR;
- `ocr/requirements-ci.txt` для Python CI;
- системные пакеты в `docker/ocr.Dockerfile`;
- базовые образы в `docker/*.Dockerfile`.

До релизного SBOM необходимо добавить генерацию CycloneDX или SPDX в CI,
сохранение artifact и проверку известных уязвимостей. Отсутствие такого artifact
является известным ограничением, а не заявлением об отсутствии уязвимостей.
