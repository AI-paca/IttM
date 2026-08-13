# SCA instructions for AI agents

Scope: every file under `.sca/`.

Do not read every generated report or the complete scan script first. The
gate already classifies the failure.

## First command

Run from the repository root:

```bash
npm run test:sca
```

This is a networked, image-building scheduled/manual gate. It performs
`npm audit`, Trivy source scanning with dev dependencies, and Trivy
vulnerability plus CycloneDX scans for four current images.

Generated `.sca/*.json` and `.sca/*.txt` files are evidence and must not be
edited or committed. The only tracked policy data is `accepted-risk.json`.

## Scope, artifacts, and responsibility

| Scope                  | Primary artifacts                                              | Blocking responsibility                                 |
| ---------------------- | -------------------------------------------------------------- | ------------------------------------------------------- |
| npm lockfile           | `.sca/npm-audit.json`                                          | Any `npm audit` failure                                 |
| Repository source      | `.sca/source-vuln.json`, `.sca/source.cdx.json`                | Source `HIGH` or `CRITICAL`, including dev dependencies |
| Gateway image          | `.sca/gateway-vuln.json`, `.sca/gateway.cdx.json`              | Fixable image `MEDIUM`, `HIGH`, or `CRITICAL`           |
| Nginx image            | `.sca/nginx-vuln.json`, `.sca/nginx.cdx.json`                  | Fixable image `MEDIUM`, `HIGH`, or `CRITICAL`           |
| OCR runtime image      | `.sca/ocr-vuln.json`, `.sca/ocr.cdx.json`                      | Fixable image `MEDIUM`, `HIGH`, or `CRITICAL`           |
| OCR CI image           | `.sca/ocr-ci-vuln.json`, `.sca/ocr-ci.cdx.json`                | Fixable image `MEDIUM`, `HIGH`, or `CRITICAL`           |
| Unfixed image families | `.sca/accepted-risk-current.txt`, `-missing.txt`, `-stale.txt` | New or disappeared family relative to tracked policy    |

The image definitions are:

| Image   | Owning source                                                     | Runtime responsibility                         |
| ------- | ----------------------------------------------------------------- | ---------------------------------------------- |
| gateway | `docker/gateway.Dockerfile`, `package-lock.json`, `gateway/`      | bundled task API and Node runtime              |
| nginx   | `docker/nginx.Dockerfile`, `gateway/nginx.conf`, web build        | only published Compose ingress and static UI   |
| ocr     | `docker/ocr.Dockerfile`, `ocr/requirements-light.txt`, `ocr/app`  | immutable production Python OCR                |
| ocr-ci  | test target of `docker/ocr.Dockerfile`, `ocr/requirements-ci.txt` | Python tests and style tooling, not production |

Optional EasyOCR/Torch packages and models installed after container startup
live in named volumes. They are not part of the immutable `ocr` image SBOM; do
not claim that this gate scanned the post-install volume.

## Companion tests and their responsibility

After changing a dependency, base image, install step, or package manifest,
run the rows owned by that scope before the full SCA gate.

| Changed scope                         | Test or command                                                                                                   | Responsibility                                                          |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `package.json` or `package-lock.json` | `npm test && npm run typecheck && npm run build`                                                                  | Node dependency compatibility for web/gateway/edge                      |
| Gateway Dockerfile/runtime packages   | `npm run test:smoke`                                                                                              | bundled server, routes and static files in the current runtime contract |
| Nginx Dockerfile/config/web assets    | `docker compose config --quiet` and `npm run test:compose`                                                        | Compose wiring, healthchecks, ingress and static delivery               |
| OCR runtime packages/Dockerfile       | `npm run test:contract && npm run test:smoke`                                                                     | Python API, upload path and gateway/Python contract                     |
| OCR CI requirements/test target       | `docker run --rm ittm-ocr-ci python -m pytest tests/api tests/engines tests/layout tests/pipeline tests/debug -q` | exact fast Python suites used by CI                                     |
| Optional EasyOCR install path         | `cd ocr && python -m pytest tests/engines/test_auto_engine.py tests/engines/test_pipeline_flags.py -q`            | engine selection and effective pipeline configuration                   |
| Accepted-risk policy                  | `npm run test:sca` only after a fresh full scan                                                                   | exact current unfixed image family set                                  |
| SCA workflow/script                   | `npm run test:sca`                                                                                                | image set, thresholds, reports and accepted-risk comparison             |

Do not use `SCA_SKIP_BUILD=1` after changing code, a Dockerfile, requirements,
or a lockfile. It is valid only when all four configured local image tags were
built from the exact current source.

## Triage by failure class

1. `npm audit`: update or replace the dependency and run Node companion tests.
2. Source `HIGH`/`CRITICAL`: inspect the reported package and dependency path.
3. Fixable image `MEDIUM+`: update the base image, OS package, or Python/npm
   dependency; rebuild and retest that image.
4. New unfixed family: review the exact CVE/package/image before policy change.
5. Stale accepted family: remove it from policy after confirming fresh reports.
6. Registry, DNS, mirror, or daemon error: repair the environment and rerun;
   never change thresholds or policy because a scan could not complete.

Open only the report named by the failing scope. Use `jq` to select the package
instead of loading the whole JSON:

```bash
jq '.Results[]?.Vulnerabilities[]? | select(.PkgName == "<package>")' \
  .sca/<scope>-vuln.json
```

## Accepted risk

`accepted-risk.json` may contain only image package families whose current
finding has no `FixedVersion`. It cannot suppress:

- npm audit;
- source findings;
- fixable image findings;
- a new unreviewed family.

A policy edit requires a fresh full `npm run test:sca`, exact package/image
review, and updated rationale when the compensating controls changed. Both a
new family and a disappeared family intentionally fail the gate.

Open `scripts/ci/run-sca.sh` or `.github/workflows/sca.yml` only when the task
changes gate implementation or scheduling. For ordinary CVE remediation,
change the owning dependency or image source.
