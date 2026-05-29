# Recommendations

## Docker Review

Current Docker implementation is functional and suitable for the course goal:
the application builds and runs in isolated containers with reproducible
startup through Docker Compose. The production images were also slimmed down so
runtime containers do not carry test files or unnecessary Node dependencies.

Estimated score: **9/10, close to 10/10**.

The project already has:

- separate containers for OCR, gateway, and nginx;
- Docker Compose orchestration;
- service healthchecks;
- documented launch and troubleshooting instructions;
- slim/alpine base images where appropriate;
- dependency layers separated from application code;
- production OCR runtime without copied tests;
- standalone gateway bundle without `node_modules` in the final image;
- smaller frontend nginx image with only the Tesseract browser assets the app
  needs.

Current local image sizes:

- `ittm-gateway`: **108MB**;
- `ittm-ocr`: **715MB**;
- `ittm-nginx`: **91.1MB**;
- `ittm-ocr-ci`: **943MB**.

Remaining optional improvements before a full 10/10:

- add explicit `docker build` and `docker run` examples if the assignment
  requires commands without Compose;
- keep Docker Compose as the recommended launch path for the full
  multi-service app;
- reduce the OCR image further only with a product tradeoff: most remaining
  weight comes from OpenCV, NumPy, Pillow, Tesseract, Poppler, and Debian/Python
  runtime packages.

Hw4 (Docker-изоляция, минимизация образов, режимы запуска)

- Docker Compose оставлен основным способом запуска полного multi-service приложения
- Production-образы уменьшены: `gateway` до 108MB, `ocr` до 715MB, `nginx` до 91.1MB
- `gateway` переведен на standalone server bundle без `node_modules` в финальном образе
- Node production dependencies пересмотрены: в runtime-зависимостях остались только серверные пакеты
- Production `ocr` переведен на multi-stage build и больше не содержит `/app/tests`
- OCR CI build вынесен в отдельный `test` target с тестами, dev-инструментами и CJK fonts
- Frontend/nginx образ уменьшен за счет копирования только нужных Tesseract browser assets
- В Compose добавлены build target/args для OCR и настраиваемая build-сеть `DOCKER_BUILD_NETWORK`
- Проверены `docker compose build`, `docker compose up`, healthcheck'и, Node tests/build и OCR lint/tests
