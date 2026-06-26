# SBOM and Dependencies

[Русский](../ru/sbom-report.md) | [Documentation](./README.md)

Analysis date: June 26, 2026.

Local SCA builds Docker images and runs `apt-get update`, `apt-get upgrade`,
and `apk upgrade` during image builds. If a corporate network, VPN, or proxy
breaks Docker DNS and you see `Temporary failure resolving deb.debian.org`,
restart the Docker daemon, wait for it to become ready, and rerun
`npm run test:sca`; that is a build-network issue, not a code finding.

The project now has a reproducible SCA/SBOM flow:

```bash
npm run test:sca
```

The command runs `npm audit`, scans the source tree with Trivy, builds the
gateway, nginx, OCR runtime, and OCR CI images, then emits vulnerability reports
and CycloneDX SBOM files under `.sca/`. The GitHub workflow
`SCA and SBOM` runs weekly and on manual dispatch, uploading `.sca/*.json` as a
30-day artifact.

The current SCA gate has no fixable source or image findings. Fixable OS
findings discovered during the run were closed with build-time package upgrades:
Alpine `libexpat` in the nginx image and Debian `libssh2-1t64` in the OCR
images.

Tracked policy lives in `.sca/accepted-risk.json`; generated reports are ignored
locally. See the Russian report for the full course-facing analysis, accepted
risk rationale, and runtime EasyOCR limitation.
