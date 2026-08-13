# Видение развития IttM

[История по commit anchors](./history.md) |
[Текущее развитие](./development-branches.md) |
[Архитектура](../architecture.md)

Этот документ сохраняет направления после текущего маркера roadmap. Это не
обещание релиза: направление становится текущей возможностью только после
production wiring, resource limits, diagnostics и tests.

## Будущее

### Browser extension

В коде есть тестируемые библиотеки `web/src/extension-core`, но нет manifest,
permissions, package и browser E2E. Готовая версия должна:

- явно запрашивать минимальные permissions;
- передавать документ только выбранному local/provider path;
- ограничивать capture и размер сохраняемого состояния;
- иметь собираемый artifact и E2E на поддерживаемом браузере.

### Новые источники

HTML canvas и файлы Google AI Studio требуют отдельных source adapters,
ограничений размера и regression fixtures. Наличие DOM helper или file input
не означает поддержку такого источника.

### Public sparse profile

`SparsePipelineRuntime` уже создаёт matrix, objects, blocks и assembled result
в debug runner, но публичные FastAPI routes используют `convert_service`.
Подключение требует версионированной artifact schema, resource bounds и
API/contract/quality tests.

## Далёкое будущее

### Hyprland capture/clipboard

Ручной `grim/slurp → curl → wl-copy` pipe работает как композиция внешних
команд. Репозиторий пока не предоставляет capture UI, scroll stitching,
Hyprland package или desktop lifecycle. Это отдельный продуктовый контур.

### Non-local deployment

Локальный API не имеет authentication, tenant isolation, rate limiting,
retention и object lifecycle. Публикация его в недоверенную сеть требует новой
security boundary.

## За пределами текущего scope

- durable queue/retry/retention и object storage;
- unattended scraping скрытых DOM-узлов;
- ввод или хранение платёжных/персональных данных;
- remote executable selector configuration.

Такие направления нельзя добавлять только документацией: сначала меняется
product scope и threat model.
