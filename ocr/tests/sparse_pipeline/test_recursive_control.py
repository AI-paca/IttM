from __future__ import annotations

import ast
import random
from pathlib import Path

import pytest

from app.sparse_pipeline.bounded_grammar import BoundedGrammar
from app.sparse_pipeline.recursive_control import (
    EvidenceAtom,
    Expand,
    FailureMode,
    PipelineInvariantError,
    PipelineLimitError,
    RecursiveWhileReducer,
    ReductionProposal,
    RunLimits,
    RunStatus,
    Terminal,
    WorkNode,
)


def node(
    node_id: str,
    rank: int,
    *,
    scope: str = "object-1",
    order: int = 0,
    coverage: tuple[str, ...] = (),
) -> WorkNode[str]:
    return WorkNode(
        node_id=node_id,
        payload=node_id,
        rank=(rank,),
        scope_id=scope,
        order_key=(order,),
        coverage_atom_ids=coverage,
    )


def atom(atom_id: str) -> EvidenceAtom[str]:
    return EvidenceAtom(
        atom_id=atom_id,
        payload=f"text:{atom_id}",
        order_key=(int(atom_id.split("-")[-1]),),
        source_id="sample",
    )


class MappingSplitter:
    def __init__(self, decisions: dict[str, object]) -> None:
        self.decisions = decisions
        self.calls: list[str] = []

    def split(self, work_node: WorkNode[str], context: object) -> object:
        del context
        self.calls.append(work_node.node_id)
        return self.decisions[work_node.node_id]


def run_tree(
    root: WorkNode[str],
    decisions: dict[str, object],
    **kwargs: object,
):
    splitter = MappingSplitter(decisions)
    runner = RecursiveWhileReducer(splitter=splitter, grammar=BoundedGrammar(), **kwargs)
    expected_evidence = tuple(atom(atom_id) for atom_id in root.coverage_atom_ids)
    return runner.run(root, expected_evidence=expected_evidence), splitter


def test_terminal_root_keeps_exact_evidence() -> None:
    root = node("root", 0, coverage=("atom-0",))
    outcome, splitter = run_tree(root, {"root": Terminal(atoms=(atom("atom-0"),), reason="leaf")})

    assert outcome.status is RunStatus.COMPLETE
    assert outcome.root is not None
    assert outcome.root.atom_ids == ("atom-0",)
    assert tuple(item.payload for item in outcome.evidence) == ("text:atom-0",)
    assert outcome.root.child_result_ids == ()
    assert outcome.steps == 1
    assert splitter.calls == ["root"]
    assert [event.event for event in outcome.trace] == ["ENTER", "TERMINAL", "EXIT"]


def test_balanced_tree_descends_and_reduces_in_exact_post_order() -> None:
    root = node("root", 3, coverage=("atom-0", "atom-1", "atom-2"))
    left = node("left", 2, order=0, coverage=("atom-0", "atom-1"))
    right = node("right", 2, order=1, coverage=("atom-2",))
    left_1 = node("left-1", 1, order=0, coverage=("atom-0",))
    left_2 = node("left-2", 1, order=1, coverage=("atom-1",))
    decisions = {
        "root": Expand("root-split", (left, right)),
        "left": Expand("left-split", (left_1, left_2)),
        "left-1": Terminal((atom("atom-0"),), "leaf"),
        "left-2": Terminal((atom("atom-1"),), "leaf"),
        "right": Terminal((atom("atom-2"),), "leaf"),
    }

    outcome, splitter = run_tree(root, decisions)

    assert outcome.root is not None
    assert outcome.root.atom_ids == ("atom-0", "atom-1", "atom-2")
    assert [item.node_id for item in outcome.derivations] == ["left-1", "left-2", "left", "right", "root"]
    assert splitter.calls == ["root", "left", "left-1", "left-2", "right"]
    assert [event.node_id for event in outcome.trace if event.event == "EXIT"] == [
        "left-1",
        "left-2",
        "left",
        "right",
        "root",
    ]


def test_deep_tree_does_not_use_python_recursion() -> None:
    depth = 1_500
    nodes = [node(f"node-{index}", depth - index, coverage=("atom-0",)) for index in range(depth + 1)]
    decisions: dict[str, object] = {
        current.node_id: Expand("descend", (nodes[index + 1],)) for index, current in enumerate(nodes[:-1])
    }
    decisions[nodes[-1].node_id] = Terminal((atom("atom-0"),), "bottom")

    outcome, splitter = run_tree(
        nodes[0],
        decisions,
        limits=RunLimits(max_nodes=depth + 1, max_depth=depth, max_children=1, max_steps=2 * depth + 1),
    )

    assert outcome.root is not None
    assert outcome.root.atom_ids == ("atom-0",)
    assert outcome.steps == 2 * depth + 1
    assert len(splitter.calls) == depth + 1


@pytest.mark.parametrize(
    "decision, message",
    [
        (Expand("empty", ()), "at least one child"),
        (Expand("same-rank", (node("child", 2),)), "rank must decrease"),
    ],
)
def test_invalid_expansion_is_rejected(decision: object, message: str) -> None:
    with pytest.raises(PipelineInvariantError, match=message):
        run_tree(node("root", 2), {"root": decision})


def test_reused_node_id_rejects_cycles_and_multiple_parents() -> None:
    root = node("root", 3)
    child = node("child", 2)
    repeated_root = node("root", 1)
    decisions = {
        "root": Expand("first", (child,)),
        "child": Expand("cycle", (repeated_root,)),
    }

    with pytest.raises(PipelineInvariantError, match="reused node ID"):
        run_tree(root, decisions)


def test_duplicate_atom_is_rejected_across_leaves() -> None:
    root = node("root", 2, coverage=("atom-0", "atom-1"))
    left = node("left", 1, order=0, coverage=("atom-0",))
    right = node("right", 1, order=1, coverage=("atom-1",))
    duplicate = atom("atom-0")
    decisions = {
        "root": Expand("pair", (left, right)),
        "left": Terminal((duplicate,), "leaf"),
        "right": Terminal((duplicate,), "leaf"),
    }

    with pytest.raises(PipelineInvariantError, match="terminal does not exactly cover"):
        run_tree(root, decisions)


class ReorderingGrammar:
    def reduce(self, work_node: WorkNode[str], children: tuple, budget: object) -> ReductionProposal:
        del work_node, budget
        return ReductionProposal(
            rule_id="bad:reverse",
            kind="sequence",
            child_result_ids=tuple(child.result_id for child in reversed(children)),
        )


def test_grammar_cannot_reorder_or_drop_children() -> None:
    root = node("root", 2, coverage=("atom-0", "atom-1"))
    left = node("left", 1, order=0, coverage=("atom-0",))
    right = node("right", 1, order=1, coverage=("atom-1",))
    splitter = MappingSplitter(
        {
            "root": Expand("pair", (left, right)),
            "left": Terminal((atom("atom-0"),), "leaf"),
            "right": Terminal((atom("atom-1"),), "leaf"),
        }
    )
    runner = RecursiveWhileReducer(splitter=splitter, grammar=ReorderingGrammar())

    with pytest.raises(PipelineInvariantError, match="changed child coverage/order"):
        runner.run(root, expected_evidence=(atom("atom-0"), atom("atom-1")))


class ExplodingGrammar:
    def reduce(self, work_node: WorkNode[str], children: tuple, budget: object) -> ReductionProposal:
        del work_node, children, budget
        raise RuntimeError("synthetic grammar failure")


def test_grammar_failure_has_lossless_opaque_fallback_in_recover_mode() -> None:
    root = node("root", 2, coverage=("atom-0", "atom-1"))
    left = node("left", 1, order=0, coverage=("atom-0",))
    right = node("right", 1, order=1, coverage=("atom-1",))
    splitter = MappingSplitter(
        {
            "root": Expand("pair", (left, right)),
            "left": Terminal((atom("atom-0"),), "leaf"),
            "right": Terminal((atom("atom-1"),), "leaf"),
        }
    )
    runner = RecursiveWhileReducer(
        splitter=splitter,
        grammar=ExplodingGrammar(),
        failure_mode=FailureMode.RECOVER,
        opaque_leaf_factory=lambda work_node, reason: Terminal((atom(f"atom-{work_node.rank[0] + 10}"),), reason),
    )

    outcome = runner.run(root, expected_evidence=(atom("atom-0"), atom("atom-1")))

    assert outcome.root is not None
    assert outcome.root.fallback is True
    assert outcome.root.kind == "sequence"
    assert outcome.root.atom_ids == ("atom-0", "atom-1")
    assert any(event.event == "FALLBACK" and event.node_id == "root" for event in outcome.trace)


class ExplodingSplitter:
    def split(self, work_node: WorkNode[str], context: object) -> object:
        del work_node, context
        raise RuntimeError("synthetic split failure")


def test_split_failure_becomes_evidenced_terminal_in_recover_mode() -> None:
    root = node("root", 2, coverage=("atom-0",))
    runner = RecursiveWhileReducer(
        splitter=ExplodingSplitter(),
        grammar=BoundedGrammar(),
        failure_mode=FailureMode.RECOVER,
        opaque_leaf_factory=lambda work_node, reason: Terminal((atom("atom-0"),), reason),
    )

    outcome = runner.run(root, expected_evidence=(atom("atom-0"),))

    assert outcome.root is not None
    assert outcome.root.fallback is True
    assert outcome.root.atom_ids == ("atom-0",)


def test_depth_and_step_limits_stop_deterministically() -> None:
    root = node("root", 2)
    child = node("child", 1)
    decisions = {"root": Expand("too-deep", (child,))}
    with pytest.raises(PipelineLimitError, match="max_depth"):
        run_tree(root, decisions, limits=RunLimits(max_nodes=2, max_depth=0, max_children=1, max_steps=2))

    step_limited = {
        "root": Expand("one-more-step", (child,)),
        "child": Terminal((atom("atom-0"),), "leaf"),
    }
    with pytest.raises(PipelineLimitError, match="max_steps"):
        run_tree(root, step_limited, limits=RunLimits(max_nodes=2, max_depth=1, max_children=1, max_steps=1))


def test_cancellation_returns_no_fake_root_and_lists_unresolved_stack() -> None:
    root = node("root", 2)
    left = node("left", 1, order=0)
    right = node("right", 1, order=1)
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    outcome, _ = run_tree(
        root,
        {"root": Expand("pair", (left, right))},
        cancelled=cancelled,
    )

    assert outcome.status is RunStatus.CANCELLED
    assert outcome.root is None
    assert outcome.derivations == ()
    assert outcome.evidence == ()
    assert outcome.unresolved_node_ids == ("root",)


def test_one_thousand_generated_trees_preserve_dfs_atom_cover() -> None:
    rng = random.Random(20260719)
    for case in range(1_000):
        counter = 0
        decisions: dict[str, object] = {}
        expected_atoms: list[str] = []

        def build(rank: int) -> WorkNode[str]:
            nonlocal counter
            node_id = f"case-{case}-node-{counter}"
            counter += 1
            if rank == 0 or rng.random() < 0.45:
                atom_id = f"atom-{counter + case * 100}"
                current = node(node_id, rank, coverage=(atom_id,))
                decisions[node_id] = Terminal((atom(atom_id),), "generated-leaf")
                expected_atoms.append(atom_id)
                return current
            built_children = tuple(build(rank - 1) for _ in range(rng.randint(1, 3)))
            children = tuple(
                WorkNode(
                    node_id=child.node_id,
                    payload=child.payload,
                    rank=child.rank,
                    scope_id=child.scope_id,
                    order_key=(index,),
                    coverage_atom_ids=child.coverage_atom_ids,
                )
                for index, child in enumerate(built_children)
            )
            coverage = tuple(atom_id for child in children for atom_id in child.coverage_atom_ids)
            current = node(node_id, rank, coverage=coverage)
            decisions[node_id] = Expand("generated-split", children)
            return current

        root = build(rng.randint(1, 5))
        outcome, _ = run_tree(
            root,
            decisions,
            limits=RunLimits(max_nodes=500, max_depth=5, max_children=3, max_steps=1_000),
        )

        assert outcome.root is not None
        assert outcome.root.atom_ids == tuple(expected_atoms)
        assert outcome.steps <= 2 * len(decisions)


def test_stage_three_has_no_image_or_ocr_dependencies() -> None:
    package = Path(__file__).parents[2] / "app" / "sparse_pipeline"
    stage_files = [
        package / "recursive_control.py",
        package / "bounded_grammar.py",
        package / "control_artifacts.py",
    ]
    forbidden_roots = {"PIL", "cv2", "numpy", "easyocr", "pytesseract"}

    for path in stage_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for statement in ast.walk(tree):
            if isinstance(statement, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in statement.names)
            elif isinstance(statement, ast.ImportFrom) and statement.module:
                imported.add(statement.module.split(".")[0])
        assert imported.isdisjoint(forbidden_roots), path
