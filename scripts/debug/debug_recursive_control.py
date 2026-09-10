#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
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
    GrammarContext,
    RecursiveWhileReducer,
    RunLimits,
    Terminal,
    WorkNode,
)

KNOWN_TEXT = {
    "p-line-1": "Первый абзац.",
    "p-line-2": "Second paragraph line.",
    "list-item-1": "- 第一项",
    "list-item-2": "- Второй пункт",
    "table-row-1": "Name | Value",
    "table-row-2": "Alpha | 42",
}


class KnownTreeSplitter:
    def __init__(self, observed_text: dict[str, str] | None = None) -> None:
        self.observed_text = dict(observed_text or KNOWN_TEXT)
        p1 = "evidence:p-line-1"
        p2 = "evidence:p-line-2"
        l1 = "evidence:list-item-1"
        l2 = "evidence:list-item-2"
        t1 = "evidence:table-row-1"
        t2 = "evidence:table-row-2"
        self.nodes = {
            "document": WorkNode(
                "document",
                "unlabeled",
                (3,),
                "document",
                (0,),
                (p1, p2, l1, l2, t1, t2),
            ),
            "paragraph": WorkNode("paragraph", "unlabeled", (2,), "paragraph-1", (0,), (p1, p2)),
            "list": WorkNode("list", "unlabeled", (2,), "list-1", (1,), (l1, l2)),
            "table": WorkNode("table", "unlabeled", (2,), "table-1", (2,), (t1, t2)),
            "p-line-1": WorkNode("p-line-1", "unlabeled", (1,), "paragraph-1", (0,), (p1,)),
            "p-line-2": WorkNode("p-line-2", "unlabeled", (1,), "paragraph-1", (1,), (p2,)),
            "list-item-1": WorkNode("list-item-1", "unlabeled", (1,), "list-1", (0,), (l1,)),
            "list-item-2": WorkNode("list-item-2", "unlabeled", (1,), "list-1", (1,), (l2,)),
            "table-row-1": WorkNode("table-row-1", "unlabeled", (1,), "table-1", (0,), (t1,)),
            "table-row-2": WorkNode("table-row-2", "unlabeled", (1,), "table-1", (1,), (t2,)),
        }

    @property
    def root(self) -> WorkNode[str]:
        return self.nodes["document"]

    def split(self, node: WorkNode[str], context: object) -> object:
        del context
        if node.node_id == "document":
            return Expand(
                "document-objects",
                (self.nodes["paragraph"], self.nodes["list"], self.nodes["table"]),
            )
        if node.node_id == "paragraph":
            return Expand("paragraph-lines", (self.nodes["p-line-1"], self.nodes["p-line-2"]))
        if node.node_id == "list":
            return Expand("list-items", (self.nodes["list-item-1"], self.nodes["list-item-2"]))
        if node.node_id == "table":
            return Expand("table-rows", (self.nodes["table-row-1"], self.nodes["table-row-2"]))

        text = self.observed_text[node.node_id]
        evidence = EvidenceAtom(
            atom_id=f"evidence:{node.node_id}",
            payload=text,
            order_key=(tuple(KNOWN_TEXT).index(node.node_id),),
            source_id="known-stage-3-sample",
        )
        return Terminal((evidence,), "known-text")


def classify(
    node: WorkNode[str],
    children: tuple,
    context: GrammarContext[str],
) -> GrammarDecision:
    del children
    if node.scope_id == "document":
        return GrammarDecision("sample:document-sequence", "sequence")

    texts = tuple(str(atom.payload).strip() for atom in context.evidence.atoms)
    if texts and all(text.startswith(("-", "*", "•")) for text in texts):
        return GrammarDecision("sample:observed-list-markers", "list")
    if texts and all("|" in text for text in texts):
        return GrammarDecision("sample:observed-table-columns", "table")
    return GrammarDecision("sample:observed-paragraph-lines", "paragraph")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the image-free recursive control sample")
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "debug" / "tmp" / "recursive-control",
        help="parent directory for immutable debug runs",
    )
    parser.add_argument("--run-id", default="stage3-known-tree", help="unique deterministic run identifier")
    parser.add_argument(
        "--corrupt-sample",
        action="store_true",
        help="intentionally corrupt one emitted atom to verify failure artifacts",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    observed_text = dict(KNOWN_TEXT)
    if args.corrupt_sample:
        observed_text["p-line-1"] = "CORRUPTED"
    splitter = KnownTreeSplitter(observed_text)
    reducer = RecursiveWhileReducer(
        splitter=splitter,
        grammar=BoundedGrammar(classify),
        limits=RunLimits(max_nodes=10, max_depth=2, max_children=3, max_steps=14),
    )
    expected_evidence = tuple(
        EvidenceAtom(
            atom_id=f"evidence:{node_id}",
            payload=text,
            order_key=(index,),
            source_id="known-stage-3-sample",
        )
        for index, (node_id, text) in enumerate(KNOWN_TEXT.items())
    )
    writer = ControlArtifactWriter()
    try:
        outcome = reducer.run(splitter.root, expected_evidence=expected_evidence)
    except Exception as exc:
        failed_outcome = getattr(exc, "outcome", None)
        if failed_outcome is not None:
            run_dir = writer.write(args.output, run_id=args.run_id, outcome=failed_outcome)
            print(run_dir)
        raise
    run_dir = writer.write(args.output, run_id=args.run_id, outcome=outcome)
    print(run_dir)
    print("atom_ids=" + ",".join(outcome.root.atom_ids if outcome.root is not None else ()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
