from __future__ import annotations

import csv
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from app.sparse_pipeline.bounded_grammar import BoundedGrammar
from app.sparse_pipeline.recursive_control import (
    EvidenceAtom,
    Expand,
    GrammarBudget,
    PipelineCancelled,
    PipelineInvariantError,
    RecursiveWhileReducer,
    ReductionProposal,
    RunLimits,
    RunStatus,
    Terminal,
    WorkNode,
)


def test_global_evidence_order_cannot_be_reversed_across_terminal_boundaries() -> None:
    """A leaf-local sort must not hide a globally reversed document order."""

    atom_a = EvidenceAtom("a", "A", (0,), "reference")
    atom_b = EvidenceAtom("b", "B", (1,), "reference")
    left = WorkNode("left", "leaf", (1,), "object", (0,), ("b",))
    right = WorkNode("right", "leaf", (1,), "object", (1,), ("a",))
    root = WorkNode("root", "document", (2,), "object", (0,), ("b", "a"))
    decisions = {
        "root": Expand("two-leaves", (left, right)),
        "left": Terminal((atom_b,), "known"),
        "right": Terminal((atom_a,), "known"),
    }

    class Splitter:
        def split(self, node: WorkNode[str], context: object) -> object:
            del context
            return decisions[node.node_id]

    runner = RecursiveWhileReducer(splitter=Splitter(), grammar=BoundedGrammar())

    with pytest.raises(PipelineInvariantError, match="canonical evidence order"):
        runner.run(root, expected_evidence=(atom_b, atom_a))


@pytest.mark.parametrize("rank", [(1.5,), (math.nan,), (True,)])
def test_rank_requires_real_nonnegative_integers(rank: tuple[object, ...]) -> None:
    """NaN and bool must not bypass the strictly-decreasing rank proof."""

    with pytest.raises((TypeError, ValueError), match="rank"):
        WorkNode("root", "document", rank, "object", (0,), ())


def test_non_string_atom_ids_are_rejected_before_a_complete_run() -> None:
    """A COMPLETE outcome must always remain serializable by the debug writer."""

    with pytest.raises(TypeError, match="atom_id"):
        EvidenceAtom(1, "A", (0,), "reference")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="coverage_atom_ids"):
        WorkNode("root", "document", (0,), "object", (0,), (1,))  # type: ignore[arg-type]


def test_non_string_grammar_rule_is_rejected_before_a_complete_run() -> None:
    atom_a = EvidenceAtom("a", "A", (0,), "reference")
    atom_b = EvidenceAtom("b", "B", (1,), "reference")
    left = WorkNode("left", "leaf", (1,), "object", (0,), ("a",))
    right = WorkNode("right", "leaf", (1,), "object", (1,), ("b",))
    root = WorkNode("root", "document", (2,), "object", (0,), ("a", "b"))
    decisions = {
        "root": Expand("two-leaves", (left, right)),
        "left": Terminal((atom_a,), "known"),
        "right": Terminal((atom_b,), "known"),
    }

    class Splitter:
        def split(self, node: WorkNode[str], context: object) -> object:
            del context
            return decisions[node.node_id]

    class BytesRuleGrammar:
        def reduce(self, node: WorkNode[str], children: tuple, context: object) -> ReductionProposal:
            del node, context
            return ReductionProposal(
                rule_id=b"bytes-rule",  # type: ignore[arg-type]
                kind="sequence",
                child_result_ids=tuple(child.result_id for child in children),
            )

    runner = RecursiveWhileReducer(splitter=Splitter(), grammar=BytesRuleGrammar())

    with pytest.raises(PipelineInvariantError, match="rule_id"):
        runner.run(root, expected_evidence=(atom_a, atom_b))


@pytest.mark.parametrize("invalid_limit", [math.nan, True, 1.5])
def test_nan_bool_and_float_cannot_disable_hard_bounds(invalid_limit: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        RunLimits(
            max_nodes=invalid_limit,  # type: ignore[arg-type]
            max_depth=invalid_limit,  # type: ignore[arg-type]
            max_children=invalid_limit,  # type: ignore[arg-type]
            max_steps=invalid_limit,  # type: ignore[arg-type]
        )
    with pytest.raises((TypeError, ValueError)):
        GrammarBudget(
            max_direct_children=invalid_limit,  # type: ignore[arg-type]
            max_atoms=invalid_limit,  # type: ignore[arg-type]
        )


def test_terminal_fallback_is_an_exact_boolean() -> None:
    atom = EvidenceAtom("a", "A", (0,), "reference")

    with pytest.raises(TypeError, match="fallback"):
        Terminal((atom,), "known", fallback=b"not-a-bool")  # type: ignore[arg-type]


def test_cancellation_from_recovery_factory_is_not_reported_as_failure() -> None:
    atom = EvidenceAtom("a", "A", (0,), "reference")
    root = WorkNode("root", "document", (0,), "object", (0,), ("a",))

    class FailingSplitter:
        def split(self, node: WorkNode[str], context: object) -> object:
            del node, context
            raise RuntimeError("enter recovery")

    def cancelling_fallback(node: WorkNode[str], reason: str) -> Terminal[str]:
        del node, reason
        raise PipelineCancelled("cancelled during recovery")

    runner = RecursiveWhileReducer(
        splitter=FailingSplitter(),
        grammar=BoundedGrammar(),
        failure_mode="recover",
        opaque_leaf_factory=cancelling_fallback,
    )

    outcome = runner.run(root, expected_evidence=(atom,))

    assert outcome.status is RunStatus.CANCELLED
    assert outcome.root is None
    assert outcome.unresolved_node_ids == ("root",)
    assert outcome.trace[-1].event == "CANCEL"


def test_corpus_replay_publishes_auditable_artifacts_for_every_reference(tmp_path: Path) -> None:
    repository_root = Path(__file__).parents[3]
    reference_dir = tmp_path / "references"
    nested = reference_dir / "nested"
    nested.mkdir(parents=True)
    (reference_dir / "paragraph.md").write_text("First line\nSecond line\n", encoding="utf-8")
    (reference_dir / "list.md").write_text("- one\n- two\n", encoding="utf-8")
    (nested / "table.md").write_text("| a | b |\n| c | d |\n", encoding="utf-8")
    (nested / "empty.md").write_text("\n\n", encoding="utf-8")
    output_dir = tmp_path / "output"

    completed = subprocess.run(
        [
            sys.executable,
            str(repository_root / "scripts" / "debug" / "debug_recursive_control_corpus.py"),
            "--reference-dir",
            str(reference_dir),
            "--output",
            str(output_dir),
            "--run-id",
            "gate",
        ],
        check=False,
        cwd=repository_root,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    corpus_dir = output_dir / "gate"
    assert json.loads((corpus_dir / "summary.json").read_text(encoding="utf-8")) == {
        "failures": 0,
        "references": 4,
        "status": "complete",
    }
    with (corpus_dir / "summary.tsv").open(encoding="utf-8", newline="") as summary_file:
        rows = list(csv.DictReader(summary_file, delimiter="\t"))
    assert {row["reference"] for row in rows} == {
        "list.md",
        "nested/empty.md",
        "nested/table.md",
        "paragraph.md",
    }

    expected_files = {
        "derivations.jsonl",
        "evidence.jsonl",
        "evidence.txt",
        "manifest.json",
        "result.txt",
        "trace.jsonl",
        "trace.txt",
    }
    run_dirs = tuple(path for path in corpus_dir.iterdir() if path.is_dir())
    assert len(run_dirs) == 4
    for run_dir in run_dirs:
        stage_dir = run_dir / "00-control"
        assert {path.name for path in stage_dir.iterdir()} == expected_files
        manifest = json.loads((stage_dir / "manifest.json").read_text(encoding="utf-8"))
        evidence_lines = (stage_dir / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
        trace_lines = (stage_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        derivation_lines = (stage_dir / "derivations.jsonl").read_text(encoding="utf-8").splitlines()
        assert manifest["status"] == "complete"
        assert manifest["evidence_atoms"] == len(evidence_lines)
        assert manifest["trace_events"] == len(trace_lines)
        assert manifest["derivations"] == len(derivation_lines)
