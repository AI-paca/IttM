# Тестирование и ответственность gates

[Документация](./README.md) | [Pipeline diagnostics](./pipeline/README.md) |
[English test map](../en/testing.md)

Основной PR workflow: `.github/workflows/tests.yml`. Команда должна выбираться
по границе, которую она проверяет, а не по удобству запуска.

| Изменённая область             | Минимальная проверка                                                    | Что доказывает                                              |
| ------------------------------ | ----------------------------------------------------------------------- | ----------------------------------------------------------- |
| Web/gateway/edge TypeScript    | `npm test && npm run typecheck`                                         | Поведение и типы текущих client/API boundaries              |
| Profile/flag documentation     | `npm run test:pipeline-docs`                                            | 18 profiles и пять разрешённых override keys                |
| Gateway/Python upload contract | `npm run test:contract`                                                 | Task worker, input и Python upload boundary                 |
| Public routes                  | `npm run test:smoke`                                                    | Gateway/static/FastAPI smoke                                |
| Compose topology               | `docker compose config --quiet`                                         | Синтаксис и связи сервисов                                  |
| Compose end-to-end             | `npm run test:compose`                                                  | nginx → gateway → OCR через собранные artifacts             |
| Browser OCR quality            | `BROWSER_OCR_LANG_PATH=<tessdata> npm run test:ocr:browser`             | Реальный Tesseract.js sample; нужны `eng`, `rus`, `chi_sim` |
| Backend OCR quality            | `npm run test:ocr:quality`                                              | Generated reference corpus и quality thresholds             |
| Sparse/recognition code        | `cd ocr && python -m pytest tests/sparse_pipeline tests/recognition -q` | Непубличный sparse runtime и evidence lattices              |
| First-party security           | `npm run test:sast`                                                     | Semgrep project rules                                       |
| Dependencies/images            | `npm run test:sca`                                                      | npm audit, Trivy, SBOM и accepted risk                      |

Python fast suites в PR не включают `quality`, `recognition` и
`sparse_pipeline`. Полная карта каталогов и scheduled gates находится в
[English test map](../en/testing.md).

PNG из debug-прогона помогает найти первую неверную границу, но не доказывает
качество OCR. Для quality-утверждения нужен reference и метрика конкретного
теста.

Pages artifact проверяется только после подготовки закреплённой модели:

```bash
npm run model:browser-reviewer
npm run build:pages
npm run test:pages
```
