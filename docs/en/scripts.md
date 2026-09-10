# Script entry points

Use the smallest public wrapper that owns the task:

| Need                                            | Entry point                            |
| ----------------------------------------------- | -------------------------------------- |
| Start the local runtime                         | `scripts/runtime/run-local.sh`         |
| Build the browser-only runtime                  | `scripts/runtime/build-lite.sh`        |
| Call the extraction API                         | `npm run extract -- ...`               |
| Preserve and replay every diagnostic boundary   | `scripts/debug/debug-all-separated.sh` |
| Compare OCR engines against explicit references | `scripts/debug/debug-all.sh`           |
| Run a CI gate                                   | the matching `npm run test:*` command  |

The stage runner is the current incident entry point. Its nested Python tools
implement persisted boundaries and are not separate operator commands. The
benchmark runner is meaningful only when `--expected-root` contains a matching
`<fixture-name>.md`; an `n/a` row is not an OCR quality result.

Scripts containing `legacy`, `version`, or `v<number>` validate frozen
evidence. Their expected reuse after another 50 commits is low because they
bind versioned schemas and saved artifacts. They must not appear in a new
support procedure.

Run scripts from the repository root and use their current `--help`. Runtime
details are in [`runtime-scripts.md`](./runtime-scripts.md); the verified stage
workflow and visual artifacts are in
[`debug/EXAMPLE.md`](../../debug/EXAMPLE.md).
