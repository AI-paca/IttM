# SBOM и зависимости

[English](../en/sbom-report.md) | [Документация](./README.md) |
[Политика безопасности](./security.md) |
[SCA coverage review](./sca-coverage-review.md)

Дата анализа: 26 июня 2026 года.

## Перед локальным запуском

SCA собирает Docker images и во время build запускает `apt-get update`,
`apt-get upgrade` и `apk upgrade`. Если корпоративная сеть, VPN или proxy
ломают Docker DNS, ошибка вроде `Temporary failure resolving deb.debian.org`
относится к build network, а не к коду или finding. Сначала перезапустите
Docker daemon: в WSL это обычно restart сервиса, например
`sudo systemctl restart docker` в дистрибутиве с systemd, а не только перезапуск
клиента. Дождитесь готовности daemon и повторите `npm run test:sca`.

## Инструмент и воспроизводимый запуск

Для SCA используется Trivy в закрепленном Docker-образе:

```text
aquasec/trivy@sha256:53570e6911c2361ebe7995228088cf83a6b9b73e7f3cdca44bd8f8f425e80fa7
```

Локальный запуск:

```bash
npm run test:sca
```

Команда:

1. запускает `npm audit`;
2. сканирует `package-lock.json` вместе с dev-зависимостями;
3. собирает production gateway, production nginx, production OCR и Python CI image;
4. создает vulnerability report и CycloneDX SBOM для каждого scope;
5. сверяет unfixed image findings с tracked accepted risk;
6. отклоняет npm advisory, source `HIGH`/`CRITICAL` и исправимые image
   `MEDIUM`/`HIGH`/`CRITICAL`.

JSON-отчеты и SBOM сохраняются в игнорируемой `.sca/`. В Git отслеживается
только `.sca/accepted-risk.json`. Workflow `SCA and SBOM` запускается
еженедельно и вручную, затем публикует `.sca/*.json` как artifact на 30 дней.
Обычный PR gate не зависит от внешней vulnerability database, чтобы не ловить
случайный шум из feeds.

## Что gate-ится

| Источник                      | Порог / условие блокировки                                                  | Артефакт                   |
| ----------------------------- | --------------------------------------------------------------------------- | -------------------------- |
| `npm audit`                   | Ненулевой exit code `npm audit`                                             | `.sca/npm-audit.json`      |
| Trivy source scan             | `HIGH`/`CRITICAL`, включая dev-зависимости                                  | `.sca/source-vuln.json`    |
| Trivy image scan              | Исправимые `MEDIUM`/`HIGH`/`CRITICAL`                                       | `.sca/*-vuln.json`         |
| Trivy image scan, unfixed     | Новая или исчезнувшая package family относительно `.sca/accepted-risk.json` | `.sca/accepted-risk-*.txt` |
| Runtime after EasyOCR install | Не gate-ится в Hw7; нужен отдельный `sca-runtime` job                       | Не создается               |

`npm audit` и Trivy остаются разными источниками: npm показывает registry
advisory для lockfile, Trivy нормализует CVE по source tree и container images.
Оба отчета сохраняются, но blocking policy задается скриптом
[`scripts/ci/run-sca.sh`](../../scripts/ci/run-sca.sh).

## Границы анализа

| Scope         | Источник зависимостей                | Назначение            |
| ------------- | ------------------------------------ | --------------------- |
| Source        | `package-lock.json`, включая dev     | web/gateway/tooling   |
| Gateway image | bundled server + Node Alpine runtime | production gateway    |
| Nginx image   | static web build + nginx Alpine      | production ingress    |
| OCR image     | `requirements-light.txt` + Debian    | production OCR        |
| OCR CI image  | `requirements-ci.txt` + Debian       | test/lint-only Python |

`ocr/requirements.txt` — полный optional-профиль с EasyOCR/Torch. Он не входит
в default production image: EasyOCR может устанавливаться позднее в отдельный
volume. Поэтому immutable image SBOM не описывает пакеты, добавленные после
старта контейнера. Манифест отслеживается Dependabot, а окружение после
runtime-установки нужно сканировать отдельно.

## Runtime после EasyOCR install

После `POST /api/install-easyocr` в volume появляются Python packages и модели,
которых нет в immutable OCR image. Hw7 сознательно не запускает Trivy после
этого optional-install: job был бы длиннее и зависел бы от runtime download.

Следующий слой — отдельный scheduled/manual `sca-runtime`: поднять OCR image,
выполнить install, просканировать `/opt/ittm-python-packages` и
`/models/easyocr`, затем приложить diff к SCA artifact. До появления этого job
SBOM описывает immutable surface.

## Найдено и исправлено

Первичный `npm audit` нашел пять package-level проблем: одну high, две
moderate и две low. Они относились к `vite`, `qs`, `js-yaml`, `esbuild` и
`@babel/core`.

Lockfile обновлен в пределах существующих semver-диапазонов:

- `vite` `6.4.2 -> 6.4.3`;
- `qs` `6.15.1 -> 6.15.3`;
- `js-yaml` `4.1.1 -> 4.2.0`;
- `esbuild` `0.28.0 -> 0.28.1`;
- `@babel/core` `7.29.0 -> 7.29.7`.

Оставшийся nested `esbuild@0.27.x` шел через `tsx@4.21.0`, поэтому dev-tooling
обновлен до `tsx@4.22.4`. После этого `npm audit --audit-level=low` возвращает
`found 0 vulnerabilities`.

В gateway image findings внутри встроенного `npm` базового Node image не нужны
для production runtime: gateway запускает готовый bundle, поэтому `npm`,
`npx`, `pnpm`, `yarn` и `corepack` удалены из runtime stage.

Повторный Trivy-прогон нашел исправимые OS findings в container images:

- Alpine `libexpat` в nginx image обновлен через `apk upgrade --no-cache`;
- Debian `libssh2-1t64`, подтянутый OCR runtime dependencies, обновлен до
  `1.11.1-1+deb13u1` build-time `apt-get upgrade`.

После этого в `gateway`, `nginx`, `ocr`, `ocr-ci` и source reports не осталось
findings с `FixedVersion`.

В OCR image build/install tools закреплены отдельными ARG:

```text
pip==26.1.2
setuptools==82.0.1
wheel==0.47.0
```

Базовые Node/Python/nginx images закреплены по digest в
`docker/gateway.Dockerfile`, `docker/ocr.Dockerfile` и
`docker/nginx.Dockerfile`; OS packages обновляются во время build, чтобы
исправимые CVE из base image не попадали в runtime. OCR runtime запускается не
от root, а от пользователя `ittm` с UID/GID `10001`; writable directories для
EasyOCR вынесены в `/opt/ittm-python-packages` и `/models/easyocr`.

## Accepted risk

Tracked policy хранится в [`.sca/accepted-risk.json`](../../.sca/accepted-risk.json).
Скрипт SCA строит текущий список unfixed image package families и сравнивает его
с этим файлом. Gate падает, если появилась новая unfixed family или если
зафиксированная family больше не встречается в отчетах: оба случая требуют
явного пересмотра accepted risk.

Accepted risk относится только к Debian package families без `FixedVersion` в
vendor feed. Это не false positive и не утверждение, что системные CVE
безопасны: риск принят временно из-за отсутствия исправленной версии.

Компенсирующие меры:

- OCR работает non-root и не публикуется наружу в Compose;
- CORS запрещен по умолчанию;
- upload ограничен 128 MiB;
- decoded image ограничен 80 млн пикселей;
- PDF ограничен 100 страницами и размером render до 6000 px;
- еженедельный SCA повторяет проверку;
- Dependabot следит за npm, pip, Docker images и GitHub Actions;
- появление исправимой `MEDIUM+` версии ломает SCA gate, пока образ не обновлен.
