# История развития по commit anchors

[Текущее развитие](./development-branches.md) |
[Архитектура](../architecture.md) |
[Полный debug-пример](../../../debug/EXAMPLE.md)

Схема построена по существующим commit object и изменениям кода внутри них.
Названия учебных границ и локальных копий не являются объектами remote history
и на ось не добавляются.

![История, точки отката и планы IttM](../../assets/roadmap.svg)

## Как выбрать точку отката

| Нужное состояние                                            | Commit anchor | Что останется за границей                                             |
| ----------------------------------------------------------- | ------------- | --------------------------------------------------------------------- |
| Последнее состояние без явного восьмиэтапного контракта     | `4d6e2a6a`    | `PipelineCapabilities`, sparse stack и shared ABI                     |
| Python-контракт восьми этапов, ещё без sparse stack         | `d0e837bc`    | sparse control, geometry, objects, blocks и fusion                    |
| Полный sparse OCR stack, ещё без общего Rust native/WASM    | `79e3e81a`    | shared ABI, browser wiring и последующие parity tests                 |
| Общий Rust decision core для Python и browser               | `7e2edef0`    | browser пока не переключён на этот общий runtime                      |
| Browser подключён к общей recipe/decision логике            | `01e8c850`    | последующие test, visual debug, docs и container alignment            |
| Контракты покрыты тестами, но без resumable visual debug    | `91c947ae`    | визуальный stage runner и его artifacts                               |
| Текущий зафиксированный конец показанной последовательности | `e3f583a6`    | только незавершённая работа и будущие пути справа от красного маркера |

`4d6e2a6a` — самостоятельное усиление существующего layout/table OCR. Цепочка
`1cd82b31…79e3e81a` меняет преимущественно sparse/OCR-код, но не является
независимым набором улучшений для дерева `4d6e2a6a`: её фундамент находится в
`d0e837bc`, а начиная с `541f9597` sparse geometry импортирует native
`pipeline_core`. Поэтому удалить восемь этапов и затем целиком cherry-pick этой
цепочки без адаптации нельзя.

## Что выросло после `92c2c44b`

Порядок ниже линейный. Он восстановлен по diff, а не по формулировкам commit
message.

| Commit     | Граница ответственности в изменённом коде                           |
| ---------- | ------------------------------------------------------------------- |
| `4d6e2a6a` | Усилены существующие layout и table parsing                         |
| `d0e837bc` | Добавлены Python pipeline contract, capabilities и порядок 8 stages |
| `1cd82b31` | Добавлены sparse control, runtime и audit state                     |
| `541f9597` | Добавлены sparse geometry и topology                                |
| `11248bd2` | Добавлена реконструкция конечных document objects                   |
| `6f1f4ee8` | Добавлено планирование bounded overlapping blocks                   |
| `ad159e58` | Добавлены adaptive multilingual OCR, session, queue и RPC adapters  |
| `79e3e81a` | Добавлены evidence fusion и document assembly                       |
| `7e2edef0` | Один Rust source собирается в native `.so` и browser WASM           |
| `01e8c850` | Browser runtime подключён к общему pipeline-core                    |
| `91c947ae` | Добавлены recursive/sparse contract и parity tests                  |
| `d9a5a7d1` | Debug runner разделён на возобновляемые визуальные этапы            |
| `a81f52c5` | Документация сведена с recursive/sparse архитектурой                |
| `bc7eb661` | Выровнены container OCR dependencies                                |
| `ab55edf5` | Локальные OCR evidence исключены из истории                         |
| `e3f583a6` | Обновлены зависимости после перечисленной цепочки                   |

В SVG каждый ряд — отдельная ответственность. Перекрывающиеся красный,
оранжевый, жёлтый и зелёный блоки показывают наслоение зрелости. Соседние
anchors `4d6e2a6a`, `d0e837bc`, `79e3e81a`, `7e2edef0` и `01e8c850` оставлены
на той же единственной оси; внутренние sparse-коммиты не превращены в отдельную
таблицу или в искусственные этапы продукта.

Зелёная штриховка после красного маркера — незавершённая текущая работа. Синяя
штриховка — путь без production wiring. Ручной
`grim/slurp → curl → wl-copy` уже работает через Task API; будущим остаётся
пакетированный Hyprland UI/lifecycle. Для browser extension ещё не выбран
окончательный transport.
