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

Generated `.sca/*.json` and `.sca/*.txt` files are per-run evidence and remain
untracked. CI uploads them for 30 days. This page intentionally does not freeze
a CVE list that would become stale.

Packages and models installed into the EasyOCR named volumes after container
startup are outside the immutable OCR image SBOM. Review that runtime inventory
separately.

For a failure, open only the report named by the failing scope. AI-agent
triage and companion-test ownership are kept in
[`.sca/AGENTS.md`](../../.sca/AGENTS.md) beside the generated data.
