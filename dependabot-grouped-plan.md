# Dependabot grouped updates: implemented plan

> Статус: **применено локально**. Конфиг Dependabot переведён на grouped
> minor/patch updates, auto-merge вынесен в отдельный GitHub Actions workflow, а
> текущая npm-волна PR проверена локально через `act`.

## Что изменено

- `.github/dependabot.yml` теперь описывает 4 экосистемы:
  `npm`, `pip`, `docker`, `github-actions`.
- Для каждой экосистемы включена группа `*-minor-and-patch`:
  Dependabot собирает `minor` и `patch` updates в один PR на экосистему.
- Major updates намеренно не попадают в группы и остаются отдельными PR для
  ручного ревью.
- Расписание задано через cron: `46 18 3 * *` в `Europe/Moscow`.
- С учётом последнего коммита `2026-06-26 18:46 +03:00` первый запуск будет
  `2026-07-03 18:46 +03:00`, дальше — 3 числа каждого месяца.
- Auto-merge реализован отдельным workflow
  `.github/workflows/dependabot-auto-merge.yml`, потому что
  `auto-merge: true` не является опцией `dependabot.yml`.

## Auto-merge

Workflow `Dependabot auto-merge` запускается после успешного workflow `Tests`
через `workflow_run`. Он:

- не использует `pull_request_target`;
- не checkout-ит код PR с write-token;
- находит PR, связанный с успешным run;
- пропускает всё, что не создано `dependabot[bot]`;
- включает `gh pr merge --auto --squash` только для grouped PR, у которых title
  или branch содержит `minor-and-patch`.

Так major PR не получают auto-merge, а grouped minor/patch PR мержатся после
зелёных required checks и правил branch protection.

## Текущие npm PR

Локально через `act` был прогнан job `node-fast` для текущих dependabot-веток:

| PR/ветка                          | Результат | Причина                                                                   |
| --------------------------------- | --------- | ------------------------------------------------------------------------- |
| `eslint-10.5.0`                   | fail      | `npm ci` падает: `eslint-plugin-react@7.37.5` не поддерживает ESLint 10   |
| `react-19.2.7`                    | fail      | одиночный bump ломает тесты: `react` 19.2.7 и `react-dom` 19.2.6 mismatch |
| `motion-12.42.0`                  | pass      | `node-fast` зелёный                                                       |
| `tailwindcss-4.3.1`               | pass      | `node-fast` зелёный                                                       |
| `typescript-eslint/parser-8.62.0` | pass      | `node-fast` зелёный                                                       |

Вместо одиночных PR локально собран совместимый grouped npm bump:

- `motion` → `12.42.0`;
- `tailwindcss` и `@tailwindcss/vite` → `4.3.1`;
- `react` и `react-dom` → `19.2.7`;
- `@typescript-eslint/parser`, `@typescript-eslint/eslint-plugin`,
  `typescript-eslint` → `8.62.0`;
- `eslint` оставлен на `9.39.4`.

После merge этой grouped-правки текущие мелкие Dependabot PR должны стать
устаревшими; `eslint-10.5.0` остаётся ручным major upgrade.

## Проверки

Локально через `act` прогнан `.github/workflows/tests.yml`:

- [x] `node-fast`
- [x] `sast`
- [x] `compose-config`
- [x] `smoke`
- [x] `contract`
- [x] `python-fast`

Также проверены:

- [x] `act --validate -W .github/workflows/dependabot-auto-merge.yml`
- [x] `npx prettier --check .github/dependabot.yml .github/workflows/dependabot-auto-merge.yml dependabot-grouped-plan.md package.json package-lock.json`

## Итог

Dependabot теперь должен приносить один monthly grouped PR на экосистему для
minor/patch обновлений. Эти grouped PR получают auto-merge только после зелёного
`Tests`; major updates остаются отдельными и ручными.
