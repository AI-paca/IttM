#!/usr/bin/env python3
import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "ocr/app/pipeline_config.py"
DOC_PATH = REPO_ROOT / "docs/ru/engine/README.md"


def class_fields(tree: ast.Module, class_name: str) -> set[str]:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            }
    raise RuntimeError(f"Missing class {class_name} in {CONFIG_PATH}")


def assigned_dict_keys(tree: ast.Module, variable_name: str) -> set[str]:
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign):
            continue
        if not isinstance(node.target, ast.Name) or node.target.id != variable_name:
            continue
        if not isinstance(node.value, ast.Dict):
            raise RuntimeError(f"{variable_name} must remain a literal dict")
        return {
            key.value
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
    raise RuntimeError(f"Missing {variable_name} in {CONFIG_PATH}")


def default_parameter_keys(tree: ast.Module) -> set[str]:
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "default_parameters":
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List)):
            continue
        for pair in node.value.elts:
            if not isinstance(pair, (ast.Tuple, ast.List)) or not pair.elts:
                continue
            key = pair.elts[0]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
    return keys


def main() -> int:
    tree = ast.parse(CONFIG_PATH.read_text(encoding="utf-8"))
    documented = DOC_PATH.read_text(encoding="utf-8")
    required = (
        class_fields(tree, "OcrPipelineProfile")
        | class_fields(tree, "LayoutPipelineConfig")
        | assigned_dict_keys(tree, "OCR_PIPELINE_PROFILES")
        | default_parameter_keys(tree)
    )
    required -= {"name", "layout"}

    missing = sorted(token for token in required if f"`{token}`" not in documented)
    if missing:
        print("Undocumented OCR profile fields or profiles:")
        for token in missing:
            print(f"- {token}")
        return 1

    print(f"Engine profile documentation covers {len(required)} tokens.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
