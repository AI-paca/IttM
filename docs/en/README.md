# IttM documentation

[Root README](../../README.md) | [Русский](../ru/README.md)

The maintained operator documentation is:

| Document                                                 | Purpose                                 |
| -------------------------------------------------------- | --------------------------------------- |
| [Pipeline diagnostics](../ru/pipeline/README.md)         | Service, request and result evidence    |
| [Full pipeline example](../../debug/EXAMPLE.md)          | Read matrix, objects, blocks and result |
| [Architecture](../ru/architecture.md)                    | Runtime components and public API       |
| [Architecture limits](../ru/architecture-limitations.md) | Upload, memory, PDF and queue limits    |
| [Security](./security.md)                                | Trust boundaries and operational risk   |
| [Development](./development.md)                          | History, active work and future gates   |

Implementation references:

- [Task API](./task-api.md);
- [backend pipeline](./backend-pipeline.md);
- [sparse pipeline](./sparse-pipeline.md);
- [runtime scripts](./runtime-scripts.md);
- [debug and CI scripts](./scripts.md);
- [local OCR/VLM deployment](./ollama-deploy.md);
- [test responsibilities](./testing.md);
- [SCA and SBOM](./sbom-report.md);
- [`pipeline-core/README.md`](../../pipeline-core/README.md).

Architecture, OCR pipeline and SAST use editable Draw.io sources with exported
SVG/PNG files in [`docs/assets`](../assets/). The roadmap remains a directly
edited SVG. Per-run evidence belongs in CI artifacts. AI-agent instructions
for scanner ownership and bounded report reading are in
[`.semgrep/AGENTS.md`](../../.semgrep/AGENTS.md) and
[`.sca/AGENTS.md`](../../.sca/AGENTS.md).
