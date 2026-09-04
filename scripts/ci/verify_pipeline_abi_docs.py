#!/usr/bin/env python3
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

ABI_CONTRACT_PATHS = (
    "pipeline-core/README.md",
    "docs/en/backend-pipeline.md",
    "docs/ru/architecture.md",
    "docs/ru/architecture-unified-pipeline.md",
    "docs/ru/pipeline/README.md",
    "docs/ru/roadmap/development-branches.md",
    "docs/ru/course/COURSE_PLAN.md",
    "docs/assets/ocr-pipeline.drawio",
    "docs/assets/ocr-pipeline.svg",
    "docs/assets/project-architecture.drawio",
    "docs/assets/project-architecture.svg",
)

READINESS_CONTRACT_PATHS = (
    "ocr/app/routers/health.py",
    "ocr/tests/api/test_main.py",
    "pipeline-core/README.md",
)

ABI_CODE_DECLARATIONS = {
    "ocr/app/pipeline_core/native.py": re.compile(
        r"^PIPELINE_CORE_ABI_VERSION = (\d+)$", re.MULTILINE
    ),
    "web/src/ocr/pipeline-core.ts": re.compile(
        r"^export const PIPELINE_CORE_ABI_VERSION = (\d+);$", re.MULTILINE
    ),
    "scripts/ci/verify-pipeline-core-wasm.mjs": re.compile(
        r"ittm_pipeline_abi_version\(\) !== (\d+)"
    ),
}


def current_abi_version() -> int:
    source = (REPO_ROOT / "pipeline-core/src/lib.rs").read_text(encoding="utf-8")
    match = re.search(
        r"^(?:pub )?const ABI_VERSION: u32 = (\d+);$", source, re.MULTILINE
    )
    if not match:
        raise RuntimeError("pipeline-core ABI_VERSION declaration was not found")
    return int(match.group(1))


def main() -> int:
    version = current_abi_version()
    failures: list[str] = []
    declaration_pattern = re.compile(r"\bABI\s+v?(\d+)\b", re.IGNORECASE)

    for relative_path, pattern in ABI_CODE_DECLARATIONS.items():
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        match = pattern.search(text)
        declared = int(match.group(1)) if match else None
        if declared != version:
            failures.append(
                f"{relative_path}: expected ABI constant {version}, found {declared}"
            )

    for relative_path in ABI_CONTRACT_PATHS:
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        declared = {int(value) for value in declaration_pattern.findall(text)}
        if declared != {version}:
            failures.append(
                f"{relative_path}: expected only ABI {version}, found {sorted(declared)}"
            )

    readiness_key = f"pipeline_core_abi{version}"
    for relative_path in READINESS_CONTRACT_PATHS:
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        if readiness_key not in text:
            failures.append(f"{relative_path}: missing `{readiness_key}`")

    if failures:
        print("Pipeline ABI documentation is out of sync:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(
        f"Pipeline ABI {version} matches maintained documentation, diagrams, "
        f"and readiness key `{readiness_key}`."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
