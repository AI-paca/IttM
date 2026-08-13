# SCA и SBOM

[Security](./security.md) | [SAST](./sast.md) |
[English](../en/sbom-report.md)

## Запуск

```bash
npm run test:sca
```

Это networked scheduled/manual gate. Он:

1. собирает текущие `gateway`, `nginx`, `ocr` и `ocr-ci` images;
2. запускает `npm audit`;
3. сканирует source с dev dependencies;
4. создаёт vulnerability JSON и CycloneDX для source и четырёх images;
5. блокирует source `HIGH/CRITICAL` и исправимые image
   `MEDIUM/HIGH/CRITICAL`;
6. сравнивает все неисправимые image package families с
   `.sca/accepted-risk.json`.

Результаты находятся в `.sca/*.json` и `.sca/*.txt`, игнорируются Git и
публикуются CI artifact на 30 дней. Это evidence конкретного запуска, поэтому
статический список CVE в документации не поддерживается.

При ошибке откройте только report указанного scope:

```bash
jq '.Results[]?.Vulnerabilities[]? | select(.PkgName == "<package>")' \
  .sca/<scope>-vuln.json
```

Optional EasyOCR/Torch packages и models, установленные после старта
контейнера в named volumes, не входят в immutable `ocr` image SBOM. Это
отдельная runtime inventory boundary.

Порог, image set и accepted-risk logic принадлежат
`scripts/ci/run-sca.sh`. Карта тестов для изменения dependencies/images
находится в [`.sca/AGENTS.md`](../../.sca/AGENTS.md).
