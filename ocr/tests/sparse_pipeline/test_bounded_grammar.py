from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from app.sparse_pipeline.bounded_grammar import BoundedGrammar, GrammarDecision
from app.sparse_pipeline.control_artifacts import ControlArtifactWriter
from app.sparse_pipeline.recursive_control import (
    Derivation,
    EvidenceAtom,
    EvidenceView,
    Expand,
    GrammarBudget,
    GrammarContext,
    RecursiveWhileReducer,
    RunLimits,
    Terminal,
    WorkNode,
)


def context(budget: GrammarBudget | None = None) -> GrammarContext[str]:
    return GrammarContext(
        budget=budget or GrammarBudget(),
        evidence=EvidenceView({}, ()),
        cancelled=lambda: False,
    )


def terminal_derivation(node_id: str, scope_id: str, atom_id: str) -> Derivation:
    return Derivation(
        result_id=f"derivation:{node_id}",
        node_id=node_id,
        scope_id=scope_id,
        evidence_scope_ids=(scope_id,),
        kind="terminal",
        child_result_ids=(),
        atom_ids=(atom_id,),
        attributes=(),
        rule_id="terminal:test",
    )


def test_classifier_can_only_choose_structure_metadata() -> None:
    seen: list[tuple[str, tuple[str, ...]]] = []

    def classify(work_node: WorkNode[str], children: tuple[Derivation, ...], context: object) -> GrammarDecision:
        del context
        seen.append((work_node.node_id, tuple(child.result_id for child in children)))
        return GrammarDecision(rule_id="classify:paragraph", kind="paragraph", attributes=(("align", "left"),))

    grammar = BoundedGrammar(classify)
    node = WorkNode("parent", "payload", (2,), "object-1", (0,), ("atom-0", "atom-1"))
    children = (
        terminal_derivation("left", "object-1", "atom-0"),
        terminal_derivation("right", "object-1", "atom-1"),
    )

    proposal = grammar.reduce(
        node,
        children,
        context(GrammarBudget(allowed_attributes=frozenset({("align", "left")}))),
    )

    assert proposal.kind == "paragraph"
    assert proposal.child_result_ids == ("derivation:left", "derivation:right")
    assert proposal.attributes == (("align", "left"),)
    assert seen == [("parent", ("derivation:left", "derivation:right"))]


def test_cross_scope_semantics_are_forced_to_neutral_sequence() -> None:
    grammar = BoundedGrammar(lambda node, children, context: GrammarDecision(rule_id="bad:table", kind="table"))
    node = WorkNode("document", "payload", (2,), "document", (0,), ("atom-0", "atom-1"))
    children = (
        terminal_derivation("left", "object-1", "atom-0"),
        terminal_derivation("right", "object-2", "atom-1"),
    )

    proposal = grammar.reduce(node, children, context())

    assert proposal.kind == "sequence"
    assert proposal.rule_id == "grammar:scope-boundary"
    assert proposal.fallback_reason is None
    assert proposal.child_result_ids == ("derivation:left", "derivation:right")


def test_budget_fallback_preserves_all_child_references() -> None:
    grammar = BoundedGrammar(lambda node, children, context: GrammarDecision(rule_id="classify:list", kind="list"))
    node = WorkNode("parent", "payload", (2,), "object-1", (0,), ("atom-0", "atom-1"))
    children = (
        terminal_derivation("left", "object-1", "atom-0"),
        terminal_derivation("right", "object-1", "atom-1"),
    )
    budget = GrammarBudget(max_direct_children=1, max_atoms=10)

    proposal = grammar.reduce(node, children, context(budget))

    assert proposal.kind == "sequence"
    assert proposal.fallback_reason == "grammar-budget"
    assert proposal.child_result_ids == ("derivation:left", "derivation:right")


def test_budget_fallback_is_accepted_by_controller_without_atom_loss() -> None:
    splitter = TinySplitter()
    runner = RecursiveWhileReducer(
        splitter=splitter,
        grammar=BoundedGrammar(),
        limits=RunLimits(max_nodes=3, max_depth=1, max_children=2, max_steps=5),
        grammar_budget=GrammarBudget(max_direct_children=1, max_atoms=10),
    )

    outcome = runner.run(
        splitter.root,
        expected_evidence=(
            EvidenceAtom("atom-0", "known-0", (0,), "synthetic"),
            EvidenceAtom("atom-1", "known-1", (1,), "synthetic"),
        ),
    )

    assert outcome.root is not None
    assert outcome.root.fallback is True
    assert outcome.root.kind == "sequence"
    assert outcome.root.atom_ids == ("atom-0", "atom-1")


class TinySplitter:
    def __init__(self) -> None:
        self.root = WorkNode("root", "root", (2,), "object-1", (0,), ("atom-0", "atom-1"))
        self.left = WorkNode("left", "left", (1,), "object-1", (0,), ("atom-0",))
        self.right = WorkNode("right", "right", (1,), "object-1", (1,), ("atom-1",))

    def split(self, node: WorkNode[str], context: object) -> object:
        del context
        if node.node_id == "root":
            return Expand("pair", (self.left, self.right))
        index = 0 if node.node_id == "left" else 1
        evidence = EvidenceAtom(f"atom-{index}", f"known-{index}", (index,), "synthetic")
        return Terminal((evidence,), "known")


def make_outcome():
    splitter = TinySplitter()
    runner = RecursiveWhileReducer(
        splitter=splitter,
        grammar=BoundedGrammar(),
        limits=RunLimits(max_nodes=3, max_depth=1, max_children=2, max_steps=5),
    )
    return runner.run(
        splitter.root,
        expected_evidence=(
            EvidenceAtom("atom-0", "known-0", (0,), "synthetic"),
            EvidenceAtom("atom-1", "known-1", (1,), "synthetic"),
        ),
    )


def test_control_artifacts_are_deterministic_and_human_readable(tmp_path: Path) -> None:
    outcome = make_outcome()
    first = ControlArtifactWriter().write(tmp_path / "first", run_id="sample", outcome=outcome)
    second = ControlArtifactWriter().write(tmp_path / "second", run_id="sample", outcome=outcome)
    first_stage = first / "00-control"
    second_stage = second / "00-control"

    assert sorted(path.name for path in first_stage.iterdir()) == [
        "derivations.jsonl",
        "evidence.jsonl",
        "evidence.txt",
        "manifest.json",
        "result.txt",
        "trace.jsonl",
        "trace.txt",
    ]
    for first_file in first_stage.iterdir():
        assert first_file.read_bytes() == (second_stage / first_file.name).read_bytes()
    assert "atom_ids=atom-0,atom-1" in (first_stage / "result.txt").read_text(encoding="utf-8")
    assert "REDUCE" in (first_stage / "trace.txt").read_text(encoding="utf-8")


def test_artifact_writer_never_overwrites_a_debug_run(tmp_path: Path) -> None:
    outcome = make_outcome()
    writer = ControlArtifactWriter()
    writer.write(tmp_path, run_id="sample", outcome=outcome)

    with pytest.raises(FileExistsError):
        writer.write(tmp_path, run_id="sample", outcome=outcome)


def test_artifact_publish_is_atomic_after_serialization_failure(tmp_path: Path) -> None:
    outcome = make_outcome()
    bad_trace = (replace(outcome.trace[0], details=(("not-json", object()),)),) + outcome.trace[1:]
    bad_outcome = replace(outcome, trace=bad_trace)
    writer = ControlArtifactWriter()

    with pytest.raises(TypeError):
        writer.write(tmp_path, run_id="sample", outcome=bad_outcome)

    assert not (tmp_path / "sample").exists()
    assert not tuple(tmp_path.glob(".sample.partial-*"))
    completed = writer.write(tmp_path, run_id="sample", outcome=outcome)
    assert (completed / "00-control" / "manifest.json").is_file()


def test_parallel_publish_has_one_winner_and_normalized_collisions(tmp_path: Path) -> None:
    outcome = make_outcome()
    writer = ControlArtifactWriter()

    def publish() -> str:
        try:
            writer.write(tmp_path, run_id="shared", outcome=outcome)
            return "published"
        except FileExistsError:
            return "exists"

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = tuple(executor.map(lambda _: publish(), range(40)))

    assert results.count("published") == 1
    assert results.count("exists") == 39
    assert not tuple(tmp_path.glob(".shared.partial-*"))


@pytest.mark.parametrize("run_id", ["../escape", "nested/path", "", "white space"])
def test_artifact_writer_rejects_unsafe_run_ids(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError):
        ControlArtifactWriter().write(tmp_path, run_id=run_id, outcome=make_outcome())
