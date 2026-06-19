# История усиления движка

[English](../en/engine-hardening-progress.md) | [Документация](./README.md)

Документ фиксирует стабильный checkpoint ветки `engine` перед merge в `main`.

## Стабильный объём

| Область          | Результат                                                             | Проверка                        |
| ---------------- | --------------------------------------------------------------------- | ------------------------------- |
| GitHub Pages     | Worker/core URL учитывают Vite base path                              | HTTP Pages verifier             |
| Локальный upload | Исходный `File` уходит напрямую в multipart без browser decode/Base64 | Web, Node и gateway tests       |
| Backend PDF      | Рендер по одной странице и логирование номера страницы                | Python tests                    |
| Decoded memory   | Image guard `80MP`, dewarp `16MP`, oversized PDF downscale            | Limit и quality tests           |
| Browser workers  | PDF, resize и Base64 вынесены из основного UI-потока                  | Worker/error/cancellation tests |
| Streaming        | Python, gateway и web передают page NDJSON без смешивания fallback    | Route/partial/truncation tests  |
| Приватность LLM  | Внешняя отправка требует явного согласия                              | Consent tests                   |
| Таблицы          | Sparse/cell fallback ограничен и сохраняет raw text                   | OCR quality A/B и backend tests |
| Benchmarks       | Добавлены повторяемые backend/browser runners                         | Отдельные scripts               |

## Независимые реализации

Browser Tesseract.js, backend Tesseract и EasyOCR не используют общий
preprocessing-код. Общими остаются названия профилей и поведенческие контракты.
Изменения Python-фильтров не должны автоматически переноситься в WASM.

## Не включено

- Экспериментальное восстановление строк диаграмм.
- Жёсткий порог 6 ГБ VRAM для EasyOCR.
- Принудительный PDF render в 6000 px.
- Глобальный запрет таблиц по числу cells: размер ограничивает только дорогой
  поклеточный fallback.
- Process-wide OCR semaphore.
- Wall-clock оптимизация под жёсткий десятисекундный бюджет.

## Проверка checkpoint от 13 июня 2026 года

- `npm run format:check`, `npm run lint`, `npm test` прошли.
- Production web/server build прошёл.
- Pages HTTP smoke подтвердил `/IttM/vendor/tesseract/worker.min.js` и четыре
  локальных asset с кодом 200.
- Backend: 43 passed, 29 skipped.
- Strict OCR quality: browser 1 passed, backend 4 passed.
- Black, Ruff и flake8 прошли.
- `docker compose config --quiet` прошёл.
- Свежая локальная сборка OCR image не завершилась из-за DNS build network;
  GitHub CI должен повторно подтвердить image build.

## Оставшиеся риски

- Python собирает upload целиком в `bytes`.
- Browser PDF сначала читает весь файл в worker `ArrayBuffer`.
- Backend отклоняет decoded images больше `80MP`; tile decoder пока отсутствует.
- После начала NDJSON HTTP status уже нельзя изменить.
- Нет distributed queue и cancellation propagation в Python OCR.
- Полный размерный набор и длительный memory soak test ещё не созданы.
