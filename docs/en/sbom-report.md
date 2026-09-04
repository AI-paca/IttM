# SCA and SBOM

[Security](./security.md) | [Russian operator page](../ru/sbom-report.md)

Run:

```bash
npm run test:sca
```

The current script builds and scans `gateway`, `nginx`, `ocr`, and `ocr-ci`;
runs `npm audit`; scans source including development dependencies; emits
vulnerability JSON and CycloneDX; gates source `HIGH/CRITICAL` and fixable
image `MEDIUM/HIGH/CRITICAL`; and reconciles unfixed image package families
with `.sca/accepted-risk.json`.

Both Cargo lockfiles are part of the source scan. The runtime images retain
the `ittm-pipeline-core` Cargo manifests beside the native library or outside
the nginx document root, so their CycloneDX reports include the Rust component.
The gate fails if either source Rust crate or any shipped Rust component is
missing from the expected SBOM.

Image scans exclude pip's embedded `pip/_vendor/bom.cdx.json`. That file is an
upstream vendoring inventory rather than a list of installed Python
distributions; scanning it as an image SBOM creates pathless duplicate
components. Installed pip, setuptools, and other runtime distributions remain
in scope.

Generated `.sca/*.json` and `.sca/*.txt` files are per-run evidence and remain
untracked. CI uploads them for 30 days. This page intentionally does not freeze
a CVE list that would become stale.

Packages and models installed into the EasyOCR named volumes after container
startup are outside the immutable OCR image SBOM. Review that runtime inventory
separately.

For a failure, open only the report named by the failing scope. AI-agent
triage and companion-test ownership are kept in
[`.sca/AGENTS.md`](../../.sca/AGENTS.md) beside the generated data.
