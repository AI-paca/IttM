# Semgrep instructions for AI agents

Scope: every file under `.semgrep/`.

The normal task is to fix the reported production code, not to understand the
whole ruleset. Never read all of `sast.yml` during routine triage.

## First command

Run from the repository root:

```bash
npm run test:sast
```

Exit code `0` is the gate. Read the rule id, source path, line, and message from
the terminal or `.sast/semgrep.json`. `.sast/semgrep.sarif` is the upload
format. Both files are generated and must not be committed.

Open only the reported rule:

```bash
rg -n -C 10 "id: <exact-rule-id>" .semgrep/sast.yml
```

Then open the reported production file around the reported line. Do not search
or load unrelated YAML sections.

## Files and responsibility

| File                            | Responsibility                                                          | Verification                                  |
| ------------------------------- | ----------------------------------------------------------------------- | --------------------------------------------- |
| `.semgrep/sast.yml`             | Stable project security rules and narrow rule exclusions                | Semgrep validate, then `npm run test:sast`    |
| `scripts/ci/run-sast.sh`        | Pinned image, scan targets, offline container, JSON/SARIF exit contract | `npm run test:sast`                           |
| `scripts/ci/summarize-sast.mjs` | Human-readable summary and fail-closed scanner/parse-error check        | `npm run test:sast`                           |
| `.github/workflows/tests.yml`   | PR/push job and artifact upload                                         | inspect `sast` job, then run the gate locally |

Default scan targets are owned by `scripts/ci/run-sast.sh`:
`docker-compose.yml`, `.github/workflows`, `gateway/nginx.conf`,
`gateway/src`, `web/src`, `edge/cloudflare-worker.ts`, `ocr/app`,
`scripts/ci`, `scripts/runtime`, `scripts/cli`, `docker`, and `server.ts`.
Adding production code outside this list requires adding its path to the gate.

## Companion tests and their responsibility

SAST detects source patterns. The paired runtime test proves the protected
behavior. Run the narrow row after changing that zone.

| Zone                                           | Test or command                                                                                                                            | Responsibility                                                   |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------- |
| Gateway routes, proxy, upload and cancellation | `node --import tsx --test gateway/src/core/routes.test.ts gateway/src/core/handle.test.ts gateway/src/tasks/task-service.test.ts`          | HTTP methods, backend forwarding, task state and cancellation    |
| Gateway filesystem/static files                | `node --import tsx --test gateway/src/services/staticFiles.test.ts`                                                                        | containment and path-traversal rejection                         |
| Gateway/node adapter and streaming             | `node --import tsx --test gateway/src/core/node-adapter.test.ts gateway/src/tasks/process-worker.test.ts`                                  | byte streaming, aborts and worker protocol                       |
| Web rendering and browser trust boundary       | `node --import tsx --test web/src/ui/MarkdownContent.test.ts web/src/ocr/llm-consent.test.ts web/src/extension-core/storage-state.test.ts` | safe Markdown rendering, external consent and secret persistence |
| Edge provider proxy/CORS                       | `node --import tsx --test edge/cloudflare-worker.test.ts`                                                                                  | origin policy, provider forwarding and error handling            |
| Python API/CORS                                | `cd ocr && python -m pytest tests/api/test_main.py -q`                                                                                     | explicit CORS, health/routes and safe error responses            |
| Python upload/decoder boundary                 | `cd ocr && python -m pytest tests/api/test_upload_processing.py -q`                                                                        | upload limits, media validation and temporary processing         |
| Compose and nginx surface                      | `docker compose config --quiet` and `node --import tsx --test gateway/src/core/compose-contract.test.ts`                                   | private service topology, healthchecks and published ingress     |
| Cross-runtime contract                         | `npm run test:contract`                                                                                                                    | gateway/Python input, layout and generated-fixture contract      |
| Public smoke path                              | `npm run test:smoke`                                                                                                                       | gateway routes, static files and Python API together             |
| Shell, Dockerfile and workflow-only finding    | `npm run test:sast` plus the nearest command above                                                                                         | source policy; no invented runtime test                          |

If local Python dependencies are unavailable, use the CI image and the same
test path:

```bash
docker run --rm ittm-ocr-ci python -m pytest tests/api/test_main.py -q
```

## Fixing a finding

1. Confirm the exact rule and source line.
2. Fix the unsafe production behavior.
3. Run the companion test from the table.
4. Run `npm run test:sast`.

Do not add `nosemgrep`, weaken severity, or exclude a path to hide a real
finding. A suppression is allowed only for a demonstrated false positive, with
a short source comment and a runtime regression test.

## Changing a rule

Only a task explicitly about the rule authorizes editing `sast.yml`. Read the
smallest matching YAML section, keep the rule id stable, and provide a clear
message, severity, CWE metadata, language, and narrow path scope.

Validate exactly:

```bash
docker run --rm -v "$PWD:/src" -w /src \
  semgrep/semgrep@sha256:c180f0c93a17b420c0af5006214a29d3c747c5459c732b740191adf657dd0068 \
  semgrep validate .semgrep/sast.yml
npm run test:sast
```

After a rule change, also run every companion test for the production zones
matched by that rule.
