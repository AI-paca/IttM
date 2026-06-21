# Видение развития проекта

[Roadmap history](./history.md) | [Development branches](./development-branches.md)

Фиксация направлений, в которых проект должен расти. Это не план релизов
и не список задач, а карта того, что мы хотим поддерживать и что **не** хотим
поддерживать. Каждое направление привязано к конкретной боли пользователя или
аудитории.

## Сценарии

| Сценарий                           | Что требуется                                                              |
| ---------------------------------- | -------------------------------------------------------------------------- |
| **Hyprland / tiling WM**           | Скриншот выделенной области → текст в `wl-copy` без браузера               |
| **Browser extension / Side Panel** | Кнопка в панели, локальный Gateway API, явный доступ к внешним провайдерам |
| **Локальная интеграция API**       | Читаемый OpenAPI/curl-контракт, логи, понятные лимиты                      |
| **Интеграция в чужой pipeline**    | Жёсткие лимиты, причины segfault/OOM, явные границы доверенной зоны        |

## Направления

### 1. Низкоуровневый длинный скриншот в Linux (native pipeline)

Сейчас длинные скриншоты сшиваются только в браузерном расширении
(`chrome.tabs.captureVisibleTab` + Canvas). На голом Hyprland/Sway этого нет.

Цель: дать нативный pipeline без браузера.

- `grim` + `slurp` для области, постраничный скролл через `swaymsg` / `hyprctl` / DBus.
- Сшивка кусков на GPU через `wlroots` DMA-BUF или fallback на CPU `Pillow` (Python).
- Pipe в Gateway API как обычный `multipart` (`Content-Type: image/png`, фрагменты конкатенируются).
- Hyprland config snippet публикуется в README как основной use case.

### 2. Browser extension (Side Panel + Content Scripts)

Side Panel = `http://localhost:<port>` внутри iframe, общается с Gateway по
тому же API. Content Scripts в whitelist-доменах добавляют кнопки.

| Домен                 | Что делает content script                            | Граница                      |
| --------------------- | ---------------------------------------------------- | ---------------------------- |
| `chatgpt.com`         | Кнопка «Скопировать ветку» → `innerText` в clipboard | DOM-only, без отправки в API |
| `claude.ai`           | То же                                                | То же                        |
| `gemini.google.com`   | То же                                                | То же                        |
| `aistudio.google.com` | Кнопка «Сохранить в Drive» через `chrome.identity`   | Только по явному действию    |

Что **не** делает content script: парсит корзины маркетплейсов, читает
скрытые DOM-узлы, делает auto-click.

### 3. Marketplace cart scrape (ассортимент) с явным allow-list

- Чёрный список по умолчанию: `amazon.*`, `ozon.*`, `wildberries.*`,
  `aliexpress.*`, любой сайт, требующий ввода платёжных данных.
- Whitelist: пользователь добавляет сайт вручную, и расширение начинает
  собирать выбранную таблицу как Markdown. По умолчанию whitelist пуст.
- В whitelist-режиме работает встроенный DLP-фильтр: 16-значные номера
  проходят Luhn-check и заменяются на `[CARD REDACTED]`, email — на
  `[EMAIL REDACTED]`, длинные цифровые строки без Luhn — оставляются.
- Полный blacklist и regex DLP публикуются в
  [docs/ru/security.md](../security.md).

### 4. Расширение движков

- **Сейчас:** Browser OCR (Tesseract.js), Local Tesseract, Local EasyOCR,
  External LLM (Gemini / OpenRouter / Ollama).
- **Цель:** общий флаг `engine_type` принимает любое имя, для которого
  зарегистрирован модуль; baseline через
  [`architecture-unified-pipeline.md`](../architecture-unified-pipeline.md).
- **Кандидаты:** PaddleOCR (китайский/японский), Surya (мультиязычный),
  GOT-OCR2 (для тяжёлых таблиц), Gemma-3 multimodal (для игрового шрифта).

### 5. Diagnostics и Observability

- `/api/diagnostics` уже существует; расширение должно собирать последние
  50 ошибок в `chrome.storage.local` и предлагать «Скачать логи» для issue.
- Circuit breaker вокруг External LLM: 5 ошибок подряд → пауза 30 секунд →
  fallback на Browser OCR.

### 6. «В чём виноват железо, в чём — код»

Каждое известное падение должно иметь публичный runbook в
[`architecture-limitations.md`](../architecture-limitations.md): сценарий,
причина, способ отличить OOM от segfault, способ отличить «документ
слишком большой» от «EasyOCR не нашёл моделей».

## Что не планируется

| Идея                                     | Почему отказ                                                                      |
| ---------------------------------------- | --------------------------------------------------------------------------------- |
| Durable async-таски с retry и retention  | Требует Redis / RabbitMQ; не входит в текущий local-first scope                   |
| Object storage / S3 / MinIO              | То же                                                                             |
| Native Wayland-композер для авто-скролла | Слишком хрупко между Hyprland / Sway / KWin (см. «Сеньор сбежал после Rust»)      |
| Парсинг DOM корзин Amazon / Ozon         | Юридический риск (PII / сессионные токены), DLP не покрывает все скрытые поля     |
| Manifest V3 remote-config для селекторов | Chrome Web Store запрещает remote code execution                                  |
| 100% unit-test покрытие                  | Не окупается для CV-кода; нужен golden corpus и метрики CER/WER (см. team_lead 1) |

## Где это обсуждается

- [GitHub Issues](https://github.com/AI-paca/IttM/issues) — конкретные фичи
  и баги.
- [GitHub Discussions](https://github.com/AI-paca/IttM/discussions) —
  направления развития, RFC.
- Решения, затрагивающие профиль движка или конфигурацию pipeline, идут
  через [docs/ru/engine/README.md](../engine/README.md) и его
  CI-верификатор.

## Связь с рабочими заметками

Этот документ фиксирует направления, а не задачи. Черновики задач и agent-аудит
остаются локальными рабочими материалами и не входят в репозиторий.
