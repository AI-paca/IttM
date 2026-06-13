# История усиления движка

[English](../en/engine-hardening-progress.md) | [Документация](./README.md)

Документ фиксирует стабильный checkpoint ветки `engine` перед merge в `main`.

## Стабильный объём

| Область          | Результат                                                             | Проверка                        |
| ---------------- | --------------------------------------------------------------------- | ------------------------------- |
| GitHub Pages     | Worker/core URL учитывают Vite base path                              | Production Pages verifier       |
| Локальный upload | Исходный `File` уходит напрямую в multipart без browser decode/Base64 | Web и gateway regression tests  |
| Backend PDF      | Рендер по одной странице и логирование номера страницы                | Python tests                    |
| Browser workers  | PDF, resize и Base64 вынесены из основного UI-потока                  | Worker/error/cancellation tests |
| Streaming        | Python, gateway и web передают page NDJSON                            | Route/parser/truncation tests   |
| Приватность LLM  | Внешняя отправка требует явного согласия                              | Consent tests                   |
| Таблицы          | Backend-профили отбрасывают неподтверждённые ложные сетки             | OCR quality A/B и backend tests |
| Benchmarks       | Добавлены повторяемые backend/browser runners                         | Отдельные scripts               |

## Независимые реализации

Browser Tesseract.js, backend Tesseract и EasyOCR не используют общий
preprocessing-код. Общими остаются названия профилей и поведенческие контракты.
Изменения Python-фильтров не должны автоматически переноситься в WASM.

## Не включено

- Экспериментальное восстановление строк диаграмм.
- Жёсткий порог 6 ГБ VRAM для EasyOCR.
- Принудительный PDF render в 6000 px.
- Глобальный лимит числа table cells.
- Process-wide OCR semaphore.
- Экспериментальное ограничение sparse wide-table fallback по времени.

## Проверка checkpoint от 13 июня 2026 года

- `npm run format:check`, `npm run lint`, `npm test` прошли.
- Production web/server build прошёл.
- Pages build подтвердил `/IttM/vendor/tesseract/worker.min.js` и четыре
  локальных asset.
- Backend: 37 passed, 7 skipped.
- Strict OCR quality: browser 1 passed, backend 4 passed.
- Black, Ruff и flake8 прошли.
- `docker compose config --quiet` прошёл.
- Свежая локальная сборка OCR image не завершилась из-за DNS build network;
  GitHub CI должен повторно подтвердить image build.

## Оставшиеся риски

- Python собирает upload целиком в `bytes`.
- Browser PDF сначала читает весь файл в worker `ArrayBuffer`.
- После начала NDJSON HTTP status уже нельзя изменить.
- Нет distributed queue и cancellation propagation в Python OCR.
- Полный размерный corpus и длительный memory soak test ещё не созданы.
