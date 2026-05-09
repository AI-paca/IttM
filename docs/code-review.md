# Code Review Snapshot

## Плюсы

- Проект уже закрывает несколько режимов запуска: GitHub Pages/browser OCR, локальный gateway, Node/Bun adapters и Python OCR backend.
- Frontend UI живой и практически не требует переписывания: основные сценарии загрузки, выбора движка, прогресса и копирования уже есть.
- Backend отделен от gateway, поэтому OCR-логику можно тестировать и развивать отдельно от хостинга.
- Есть Docker Compose, FastAPI health/readiness endpoints и базовая структура под CI.

## Минусы и риски

- OCR качество зависит от языковых данных, шрифтов, Tesseract.js downloads и системного Tesseract; строгие ru/en/zh тесты поэтому вынесены в отдельный heavy CI job.
- EasyOCR runtime install оставлен ради удобства, но это все еще тяжелая и не самая безопасная операция для production.
- Browser OCR может упираться в память на длинных скриншотах/PDF; добавлен diagnostics-based профиль и downscale, но реальные лимиты браузера остаются внешним фактором.
- LLM OCR зависит от сторонних API и CORS/rate limits; теперь ошибки нормализуются, но сами сервисы остаются недетерминированными.
- Docker/security/SCA пока не подтверждены отдельными проверками, поэтому README не отмечает их как выполненные.

## Что исправлено

- `App.tsx` оставлен визуально тем же, но OCR/API/LLM/file logic вынесены в тестируемые модули.
- API errors теперь парсятся из JSON/text/html и показываются без HTML-обертки платформы.
- PDF обработка теперь собирает native text и OCR image layer вместе, а не выбирает только один источник.
- Browser OCR получил строгий языковой набор `chi_sim+eng+rus`, ресурсный профиль и worker cache.
- Gateway `/api/probe` совместим с backend `/v1/probe`, dead `/api/install-light` возвращает явный JSON.
- Node adapter переведен на ESM-compatible entrypoint.
- Добавлены быстрые unit tests, Python tests, strict OCR quality tests, GitHub Actions workflow и cache-friendly `debug.sh`.
