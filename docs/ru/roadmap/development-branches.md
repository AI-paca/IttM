# Текущее развитие IttM

[Архитектура](../architecture.md) |
[Ограничения](../architecture-limitations.md) |
[Тестирование](../testing.md) |
[История по commit anchors](./history.md) |
[Видение](./vision.md) |
[Полный debug-пример](../../../debug/EXAMPLE.md)

Этот документ описывает текущее развитие и критерии будущих функций.
Историческая ось использует существующие commit object, а текущий статус
определяется production wiring и тестами.

![Развитие IttM](../../assets/roadmap.svg)

## Как читать схему

| Вид                        | Значение                                         |
| -------------------------- | ------------------------------------------------ |
| `p0`, красный              | Первый работающий прототип                       |
| `p1`, оранжевый            | Интегрирован в один runtime path                 |
| `p2`, жёлтый               | Функциональный контур, ещё без полного hardening |
| `p3`, жёлто-зелёный        | Ограничен, диагностируется и защищён проверками  |
| `p4`, зелёный              | Стабильная release shape                         |
| Зелёная штриховка          | Текущая незавершённая работа                     |
| Синяя штриховка            | Будущая возможность без реализации               |
| Подпись над осью           | Существующий сокращённый commit hash             |
| Красная вертикальная линия | Зафиксированный текущий commit anchor            |
| Синяя пунктирная линия     | Граница ещё не реализованных планов              |

Цвет и штриховка отвечают на разные вопросы. Сплошной красный блок уже
существует, но остаётся прототипом. Штрихованный блок ещё не образует
работающий путь независимо от предполагаемой зрелости.

Будущий пункт не считается реализованным, пока не появились production wiring,
ограничения ресурсов, диагностика и тесты.

## Пройденные этапы и текущее состояние

| Этап               | Что он добавил                                    | Что работает сейчас                                               |
| ------------------ | ------------------------------------------------- | ----------------------------------------------------------------- |
| MVP                | Web UI, загрузку файла и первую конвертацию       | React UI и выбор источника                                        |
| Local runtime      | nginx, gateway, Compose, Tesseract и EasyOCR      | локальный backend path через `convert_service`                    |
| PDF и структура    | text-layer shortcut, raster OCR, layout и таблицы | PNG/JPEG/WebP/PDF → Markdown                                      |
| Task clients       | очередь, events, cancellation и CLI               | in-memory Task API и `/api/extract/text`                          |
| Browser runtime    | workers, Tesseract.js и локальные assets          | Lite без backend и browser fallback                               |
| Shared raster path | восемь separated stages и единая block geometry   | `pipeline-core` ABI 4 в native и WASM                             |
| Security gates     | SAST, SCA, SBOM и accepted-risk policy            | блокирующие Semgrep/npm/Trivy gates                               |
| Sparse diagnostics | разреженную матрицу, objects, blocks и fusion     | opt-in debug runtime и [полный пример](../../../debug/EXAMPLE.md) |

`SparsePipelineRuntime` пока не создаётся публичными FastAPI routes.
`web/src/extension-core` содержит проверенные библиотеки, но manifest и
собираемого browser extension ещё нет.

## В работе

- `OCR quality`: качество multilingual OCR и reference corpus;
- `quality gates`: исправление причины упавшего gate и полный повтор;
- `support docs`: короткая документация сопровождения, сверяемая с кодом и
  реальными прогонами.

Именно эти три пункта показаны зелёной штриховкой. Остальные сплошные блоки
имеют цвет своей подтверждённой стадии, а не автоматически считаются
стабильными.

## Ближайшее развитие

1. Добавить upload limit до `formData()`/`arrayBuffer()` в gateway и удалять
   source `File` из terminal task records.
2. Версионировать sparse artifact schema и закрепить ключ
   `page/object/policy/block/job`.
3. Добавить installed-language diagnostics и multilingual reference gates.
4. Поддерживать native/WASM parity ABI 4 и отдельно определить судьбу
   generated `rust/ocr-core`, который не вызывается production browser path.

## Будущее

| Направление                    | Gate готовности                                            |
| ------------------------------ | ---------------------------------------------------------- |
| Public sparse profile          | route wiring, resource bounds, API/contract/quality tests  |
| Browser extension              | manifest, permissions review, build artifact и browser E2E |
| HTML canvas / AI Studio inputs | bounded extraction contract и source-specific tests        |

## Далёкое будущее

| Направление                | Почему это новый perimeter                                         |
| -------------------------- | ------------------------------------------------------------------ |
| Hyprland package/lifecycle | Ручной `grim/slurp → curl → wl-copy` уже работает; нет готового UI |
| Non-local deployment       | Нужны authentication, rate limit, retention и data lifecycle       |

Durable queue, object storage и unattended scraping стороннего DOM также
требуют отдельного изменения product scope и trust boundary.
