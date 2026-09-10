# IttM development

[Architecture](../ru/architecture.md) |
[Limits](../ru/architecture-limitations.md) |
[Full Russian development notes](../ru/roadmap/development-branches.md)

![IttM development roadmap](../assets/roadmap.svg)

This is one development document, not a second history page. Status comes from
current runtime wiring and tests:

- public FastAPI routes use `convert_service`;
- `SparsePipelineRuntime` is a tested diagnostic runtime without a public route;
- `web/src/extension-core` has tested libraries but no extension manifest or
  packaged browser artifact;
- the extraction CLI and a manual `grim/slurp → curl → wl-copy` composition
  work, while packaged capture UI, scroll stitching, and desktop lifecycle do
  not;
- the local API has no authentication and is not an internet deployment
  boundary.

Active work covers multilingual OCR quality, reference evidence, sparse
artifact hardening and runtime-checked diagnostics. Near-term work covers
gateway upload lifetime, terminal task-file eviction, a versioned sparse
artifact schema, installed-language diagnostics and native/WASM parity.

Public sparse routing, HTML/AI Studio inputs and a packaged browser extension
are future product work. A packaged Hyprland capture/lifecycle product and
non-local deployment are far-future work because they introduce a new runtime
and trust perimeter.
