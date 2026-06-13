# SBOM and Dependencies

[Русский](../ru/sbom-report.md) | [Documentation](./README.md)

An automatically published SBOM is not available yet.

Current dependency sources:

- `package-lock.json` for web/gateway/edge;
- `ocr/requirements-light.txt` for OCR runtime;
- `ocr/requirements-ci.txt` for Python CI;
- system packages in `docker/ocr.Dockerfile`;
- base images in `docker/*.Dockerfile`.

A release-grade setup should generate CycloneDX or SPDX in CI, retain the
artifact, and scan known vulnerabilities. The missing artifact is a documented
limitation, not evidence that the dependency tree is vulnerability-free.
