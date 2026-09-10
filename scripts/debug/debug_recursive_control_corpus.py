#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def find_python_root() -> Path:
    candidates = (
        REPOSITORY_ROOT / "ocr",
        Path.cwd() / "ocr",
        Path.cwd(),
        Path("/app"),
    )
    for candidate in candidates:
        if (candidate / "app").is_dir():
            return candidate
    raise RuntimeError("cannot locate the OCR Python package root")


sys.path.insert(0, str(find_python_root()))

from app.sparse_pipeline.bounded_grammar import BoundedGrammar, GrammarDecision  # noqa: E402
from app.sparse_pipeline.control_artifacts import ControlArtifactWriter  # noqa: E402
from app.sparse_pipeline.recursive_control import (  # noqa: E402
    EvidenceAtom,
    Expand,
    GrammarBudget,
    GrammarContext,
    RecursiveWhileReducer,
    RunLimits,
    RunStatus,
    Terminal,
    WorkNode,
)

LIST_MARKER = re.compile(r"^(?:[-+*•‣◦]|\d+[.)])\s+")
MAX_LINES_PER_BLOCK = 64


@dataclass(frozen=True)
class PreparedReference:
    root: WorkNode[str]
    expected: tuple[EvidenceAtom[str], ...]
    decisions: dict[str, object]


class PreparedSplitter:
    def __init__(self, decisions: dict[str, object]) -> None:
        self._decisions = decisions

    def split(self, node: WorkNode[str], context: object) -> object:
        del context
        return self._decisions[node.node_id]


def split_blocks(text: str) -> tuple[tuple[str, ...], ...]:
    raw_blocks = re.split(r"\n\s*\n", text)
    blocks: list[tuple[str, ...]] = []
    for raw_block in raw_blocks:
        lines = tuple(line.rstrip() for line in raw_block.splitlines() if line.strip())
        for offset in range(0, len(lines), MAX_LINES_PER_BLOCK):
            chunk = lines[offset : offset + MAX_LINES_PER_BLOCK]
            if chunk:
                blocks.append(chunk)
    return tuple(blocks)


def prepare_reference(path: Path, reference_root: Path) -> PreparedReference:
    relative = path.relative_to(reference_root).as_posix()
    file_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:12]
    blocks = split_blocks(path.read_text(encoding="utf-8"))
    expected: list[EvidenceAtom[str]] = []
    decisions: dict[str, object] = {}
    block_nodes: list[WorkNode[str]] = []
    global_order = 0

    for block_index, lines in enumerate(blocks):
        scope_id = f"{file_id}:block:{block_index:04d}"
        leaf_nodes: list[WorkNode[str]] = []
        for line_index, line in enumerate(lines):
            atom_id = f"{file_id}:line:{global_order:06d}"
            evidence = EvidenceAtom(
                atom_id=atom_id,
                payload=line,
                order_key=(global_order,),
                source_id=relative,
            )
            expected.append(evidence)
            leaf = WorkNode(
                node_id=f"{scope_id}:line:{line_index:03d}",
                payload="unlabeled",
                rank=(1,),
                scope_id=scope_id,
                order_key=(line_index,),
                coverage_atom_ids=(atom_id,),
            )
            decisions[leaf.node_id] = Terminal((evidence,), "reference-line")
            leaf_nodes.append(leaf)
            global_order += 1

        block_coverage = tuple(atom_id for leaf in leaf_nodes for atom_id in leaf.coverage_atom_ids)
        block_node = WorkNode(
            node_id=scope_id,
            payload="unlabeled",
            rank=(2,),
            scope_id=scope_id,
            order_key=(block_index,),
            coverage_atom_ids=block_coverage,
        )
        decisions[block_node.node_id] = Expand("reference-block-lines", tuple(leaf_nodes))
        block_nodes.append(block_node)

    root_coverage = tuple(atom.atom_id for atom in expected)
    root = WorkNode(
        node_id=f"{file_id}:document",
        payload="unlabeled",
        rank=(3,),
        scope_id=f"{file_id}:document",
        order_key=(0,),
        coverage_atom_ids=root_coverage,
    )
    if block_nodes:
        decisions[root.node_id] = Expand("reference-document-blocks", tuple(block_nodes))
    else:
        decisions[root.node_id] = Terminal((), "empty-reference")
    return PreparedReference(root, tuple(expected), decisions)


def classify_reference_block(
    node: WorkNode[str],
    children: tuple,
    context: GrammarContext[str],
) -> GrammarDecision:
    del node, children
    lines = tuple(str(atom.payload).strip() for atom in context.evidence.atoms)
    if len(lines) >= 2 and all("|" in line for line in lines):
        return GrammarDecision("corpus:observed-table", "table")
    if lines and all(LIST_MARKER.match(line) for line in lines):
        return GrammarDecision("corpus:observed-list", "list")
    return GrammarDecision("corpus:observed-paragraph", "paragraph")


def safe_run_id(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.name).strip("-.")[:48] or "reference"
    return f"{digest}-{stem}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay stage 3 over existing Markdown debug references")
    parser.add_argument(
        "--reference-dir",
        type=Path,
        default=REPOSITORY_ROOT / "debug" / "reference",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "debug" / "tmp" / "recursive-control-corpus",
    )
    parser.add_argument("--run-id", required=True, help="new immutable corpus-run directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reference_root = args.reference_dir.resolve()
    paths = tuple(sorted(reference_root.rglob("*.md")))
    if not paths:
        raise FileNotFoundError(f"no Markdown references below {reference_root}")

    corpus_dir = args.output.resolve() / args.run_id
    corpus_dir.mkdir(parents=True, exist_ok=False)
    rows = ["reference\tstatus\tatoms\tderivations\tsteps\tparagraphs\tlists\ttables\terror"]
    failures = 0

    for path in paths:
        prepared = prepare_reference(path, reference_root)
        max_children = max(
            1,
            max(
                (len(decision.children) for decision in prepared.decisions.values() if isinstance(decision, Expand)),
                default=1,
            ),
        )
        reducer = RecursiveWhileReducer(
            splitter=PreparedSplitter(prepared.decisions),
            grammar=BoundedGrammar(classify_reference_block),
            limits=RunLimits(
                max_nodes=len(prepared.decisions),
                max_depth=2,
                max_children=max_children,
                max_steps=max(1, 2 * len(prepared.decisions)),
            ),
            grammar_budget=GrammarBudget(
                max_direct_children=max_children,
                max_atoms=max(1, len(prepared.expected)),
            ),
        )
        item_run_id = safe_run_id(path, reference_root)
        try:
            outcome = reducer.run(prepared.root, expected_evidence=prepared.expected)
        except Exception as exc:
            failures += 1
            outcome = getattr(exc, "outcome", None)
            if outcome is None:
                raise
        else:
            if outcome.status is not RunStatus.COMPLETE:
                failures += 1
        ControlArtifactWriter().write(corpus_dir, run_id=item_run_id, outcome=outcome)
        kinds = tuple(item.kind for item in outcome.derivations)
        rows.append(
            "\t".join(
                (
                    path.relative_to(reference_root).as_posix(),
                    outcome.status.value,
                    str(len(outcome.evidence)),
                    str(len(outcome.derivations)),
                    str(outcome.steps),
                    str(kinds.count("paragraph")),
                    str(kinds.count("list")),
                    str(kinds.count("table")),
                    outcome.error or "",
                )
            )
        )

    (corpus_dir / "summary.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (corpus_dir / "summary.json").write_text(
        json.dumps(
            {
                "references": len(paths),
                "failures": failures,
                "status": RunStatus.FAILED.value if failures else RunStatus.COMPLETE.value,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(corpus_dir)
    print(f"references={len(paths)} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
