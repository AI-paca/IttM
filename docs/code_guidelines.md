## Общие требования к коду

- Код должен быть читаемым и поддерживаемым
- Соблюдать стайлгайд выбранного языка
- Использовать стандартные инструменты управления зависимостями

### Управление зависимостями по языкам

| Язык                 | Файл зависимостей                                                                                             | Инструмент     |
| -------------------- | ------------------------------------------------------------------------------------------------------------- | -------------- |
| Python               | `requirements-light.txt` для `run.sh`, `requirements-ci.txt` для проверок, `requirements.txt` для полного OCR | pip            |
| JavaScript / Node.js | `package.json`                                                                                                | npm / yarn     |
| PHP                  | `composer.json`                                                                                               | Composer       |
| Ruby                 | `Gemfile`                                                                                                     | Bundler        |
| Java / Kotlin        | `pom.xml` / `build.gradle`                                                                                    | Maven / Gradle |
| Rust                 | `Cargo.toml`                                                                                                  | Cargo          |
| Go                   | `go.mod`                                                                                                      | Go Modules     |
