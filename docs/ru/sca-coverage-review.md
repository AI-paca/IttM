# SCA/SBOM coverage review

[SBOM](./sbom-report.md) | [Политика безопасности](./security.md)

Снимок для Hw7 от 26 июня 2026 года.

## Что закрыто

| Зона                         | Реализация                                      |
| ---------------------------- | ----------------------------------------------- |
| npm lockfile                 | `npm audit`, Trivy source scan                  |
| SBOM                         | CycloneDX JSON для source и Docker images       |
| Production images            | gateway, nginx, OCR runtime                     |
| Python CI image              | OCR test/lint image                             |
| Accepted risk                | tracked `.sca/accepted-risk.json`               |
| CI                           | scheduled/manual `.github/workflows/sca.yml`    |
| Dependency update monitoring | `.github/dependabot.yml` для npm/pip/docker/GHA |

SCA-гейт intentionally отделен от обычного PR workflow: vulnerability feeds
меняются независимо от кода, поэтому weekly/manual job лучше подходит для
стабильного курса.

## Что не закрыто

1. Runtime после `install-easyocr`: optional packages и модели появляются в
   volume после старта контейнера, поэтому immutable image SBOM их не видит.
2. License compliance: текущий Hw7 закрывает CVE/SBOM, но не вводит license
   policy.
3. Secret scanning истории Git: это отдельный класс проверки, не SCA.

## Следующий шаг

Добавить отдельный `sca-runtime` job: поднять OCR image, выполнить optional
EasyOCR install, просканировать `/opt/ittm-python-packages` и
`/models/easyocr`, затем приложить diff к weekly artifact.
