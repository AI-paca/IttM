from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.sparse_pipeline.bounded_grammar import BoundedGrammar, GrammarDecision
from app.sparse_pipeline.control_artifacts import ControlArtifactWriter
from app.sparse_pipeline.recursive_control import (
    EvidenceAtom,
    Expand,
    FailureMode,
    GrammarBudget,
    PipelineInvariantError,
    PipelineLimitError,
    RecursiveWhileReducer,
    ReductionProposal,
    RunLimits,
    RunStatus,
    Terminal,
    WorkNode,
)


def evidence(
    atom_id: str,
    payload: str,
    *,
    order: int = 0,
    source: str = "reference",
) -> EvidenceAtom[str]:
    return EvidenceAtom(atom_id, payload, (order,), source)


def work(
    node_id: str,
    rank: tuple[int, ...],
    coverage: tuple[str, ...],
    *,
    order: int = 0,
    scope: str = "object",
) -> WorkNode[str]:
    return WorkNode(node_id, "unlabeled", rank, scope, (order,), coverage)


class DecisionSplitter:
    def __init__(self, decisions: dict[str, object]) -> None:
        self.decisions = decisions

    def split(self, node: WorkNode[str], context: object) -> object:
        del context
        return self.decisions[node.node_id]


def execute(
    root: WorkNode[str],
    decisions: dict[str, object],
    expected: tuple[EvidenceAtom[str], ...],
    *,
    grammar: object | None = None,
    failure_mode: FailureMode | str = FailureMode.STRICT,
    limits: RunLimits | None = None,
):
    expected_by_id = {atom.atom_id: atom for atom in expected}
    runner = RecursiveWhileReducer(
        splitter=DecisionSplitter(decisions),
        grammar=grammar or BoundedGrammar(),
        failure_mode=failure_mode,
        opaque_leaf_factory=(
            (
                lambda node, reason: Terminal(
                    tuple(expected_by_id[atom_id] for atom_id in node.coverage_atom_ids),
                    reason,
                )
            )
            if FailureMode(failure_mode) is FailureMode.RECOVER
            else None
        ),
        limits=limits,
    )
    return runner.run(root, expected_evidence=expected)


@pytest.mark.parametrize(
    "actual",
    [
        evidence("a", "WRONG"),
        evidence("a", "right", source="wrong-source"),
        evidence("a", "right", order=1),
    ],
)
def test_terminal_must_match_external_reference_atom_exactly(actual: EvidenceAtom[str]) -> None:
    root = work("root", (0,), ("a",))

    with pytest.raises(PipelineInvariantError, match="changed expected evidence"):
        execute(root, {"root": Terminal((actual,), "leaf")}, (evidence("a", "right"),))


def test_mutable_evidence_payload_is_rejected_at_the_boundary() -> None:
    with pytest.raises(TypeError, match="immutable string"):
        EvidenceAtom("a", {"text": "mutable"}, (0,), "reference")


def test_nonempty_declared_coverage_cannot_be_silently_dropped() -> None:
    root = work("root", (0,), ("a",))

    with pytest.raises(PipelineInvariantError, match="terminal does not exactly cover"):
        execute(root, {"root": Terminal((), "bad-empty")}, (evidence("a", "right"),))


def test_strict_failure_keeps_partial_trace_and_writes_error_artifact(tmp_path: Path) -> None:
    root = work("root", (0,), ("a",))

    with pytest.raises(PipelineInvariantError) as caught:
        execute(root, {"root": Terminal((), "bad-empty")}, (evidence("a", "right"),))

    outcome = caught.value.outcome
    assert outcome.status is RunStatus.FAILED
    assert outcome.root is None
    assert outcome.unresolved_node_ids == ("root",)
    assert [event.event for event in outcome.trace] == ["ENTER", "ERROR"]
    run_dir = ControlArtifactWriter().write(tmp_path, run_id="failed", outcome=outcome)
    assert "terminal does not exactly cover" in (run_dir / "00-control" / "error.txt").read_text(encoding="utf-8")
    assert (run_dir / "00-control" / "error.json").is_file()


def test_terminal_duplicate_order_keys_are_rejected() -> None:
    root = work("root", (0,), ("a", "b"))
    expected = (evidence("a", "A", order=0), evidence("b", "B", order=1))
    actual = (evidence("a", "A", order=0), evidence("b", "B", order=0))

    with pytest.raises(PipelineInvariantError, match="duplicate evidence order keys"):
        execute(root, {"root": Terminal(actual, "bad-order")}, expected)


@pytest.mark.parametrize(
    "children",
    [
        (work("left", (1,), ("a",)),),
        (work("left", (1,), ("a",), order=0), work("right", (1,), ("a",), order=1)),
        (work("left", (1,), ("b",), order=0), work("right", (1,), ("a",), order=1)),
    ],
)
def test_children_must_exactly_partition_parent_coverage(children: tuple[WorkNode[str], ...]) -> None:
    root = work("root", (2,), ("a", "b"))

    with pytest.raises(PipelineInvariantError, match="exactly cover"):
        execute(
            root,
            {"root": Expand("bad-partition", children)},
            (evidence("a", "A", order=0), evidence("b", "B", order=1)),
        )


@pytest.mark.parametrize(
    "children, message",
    [
        (
            (work("left", (1,), ("a",), order=1), work("right", (1,), ("b",), order=0)),
            "canonical order keys",
        ),
        (
            (work("left", (1,), ("a",), order=0), work("right", (1,), ("b",), order=0)),
            "canonical order keys",
        ),
        ((work("child", (1, 0), ("a",)),), "rank arity changed"),
    ],
)
def test_child_order_and_rank_shape_are_canonical(
    children: tuple[WorkNode[str], ...],
    message: str,
) -> None:
    root_coverage = tuple(atom_id for child in children for atom_id in child.coverage_atom_ids)
    root = work("root", (2,), root_coverage)
    expected = tuple(evidence(atom_id, atom_id, order=index) for index, atom_id in enumerate(root_coverage))

    with pytest.raises(PipelineInvariantError, match=message):
        execute(root, {"root": Expand("bad-shape", children)}, expected)


def test_malformed_terminal_recovers_to_external_evidence() -> None:
    root = work("root", (0,), ("a",))
    expected = (evidence("a", "right"),)

    outcome = execute(
        root,
        {"root": Terminal(("not-an-evidence-atom",), "malformed")},
        expected,
        failure_mode="recover",
    )

    assert outcome.status is RunStatus.DEGRADED
    assert outcome.root is not None and outcome.root.atom_ids == ("a",)
    assert any(event.event == "FALLBACK" for event in outcome.trace)


def test_explicit_leaf_fallback_propagates_to_root_and_trace() -> None:
    root = work("root", (3,), ("a",))
    middle = work("middle", (2,), ("a",))
    leaf = work("leaf", (1,), ("a",))
    expected = (evidence("a", "right"),)
    decisions = {
        "root": Expand("root-child", (middle,)),
        "middle": Expand("middle-child", (leaf,)),
        "leaf": Terminal(expected, "low-confidence", fallback=True),
    }

    outcome = execute(root, decisions, expected)

    assert outcome.status is RunStatus.DEGRADED
    assert outcome.root is not None and outcome.root.fallback is True
    assert outcome.root.kind == "sequence"
    assert [event.node_id for event in outcome.trace if event.event == "FALLBACK"] == [
        "leaf",
        "middle",
        "root",
    ]


def test_descendant_scope_cannot_be_laundered_by_neutral_container() -> None:
    root = work("root", (3,), ("a",), scope="scope-a")
    middle = work("middle", (2,), ("a",), scope="scope-a")
    leaf = work("leaf", (1,), ("a",), scope="scope-b")
    expected = (evidence("a", "right"),)
    classifier_calls = 0

    def classify(node: WorkNode[str], children: tuple, context: object) -> GrammarDecision:
        nonlocal classifier_calls
        del node, children, context
        classifier_calls += 1
        return GrammarDecision("unsafe:table", "table")

    outcome = execute(
        root,
        {
            "root": Expand("root-child", (middle,)),
            "middle": Expand("middle-child", (leaf,)),
            "leaf": Terminal(expected, "leaf"),
        },
        expected,
        grammar=BoundedGrammar(classify),
    )

    assert outcome.status is RunStatus.COMPLETE
    assert outcome.root is not None
    assert outcome.root.kind == "sequence"
    assert outcome.root.evidence_scope_ids == ("scope-b",)
    assert classifier_calls == 0


def test_disallowed_text_attribute_becomes_neutral_fallback() -> None:
    root = work("root", (2,), ("a", "b"))
    left = work("left", (1,), ("a",), order=0)
    right = work("right", (1,), ("b",), order=1)
    expected = (evidence("a", "A", order=0), evidence("b", "B", order=1))
    grammar = BoundedGrammar(
        lambda node, children, context: GrammarDecision(
            "unsafe:attribute",
            "paragraph",
            (("text", "INVENTED"),),
        )
    )

    outcome = execute(
        root,
        {
            "root": Expand("pair", (left, right)),
            "left": Terminal((expected[0],), "leaf"),
            "right": Terminal((expected[1],), "leaf"),
        },
        expected,
        grammar=grammar,
    )

    assert outcome.status is RunStatus.DEGRADED
    assert outcome.root is not None
    assert outcome.root.kind == "sequence"
    assert outcome.root.attributes == ()


class SemanticFallbackGrammar:
    def reduce(self, node: WorkNode[str], children: tuple, context: object) -> ReductionProposal:
        del node, context
        return ReductionProposal(
            rule_id="unsafe:fallback-table",
            kind="table",
            child_result_ids=tuple(child.result_id for child in children),
            fallback_reason="unsafe",
        )


def test_custom_fallback_cannot_keep_semantic_kind() -> None:
    root = work("root", (2,), ("a",))
    leaf = work("leaf", (1,), ("a",))
    expected = (evidence("a", "A"),)

    with pytest.raises(PipelineInvariantError, match="fallback must use the neutral kind"):
        execute(
            root,
            {"root": Expand("child", (leaf,)), "leaf": Terminal(expected, "leaf")},
            expected,
            grammar=SemanticFallbackGrammar(),
        )


def test_terminal_kind_cannot_be_enabled_for_internal_grammar() -> None:
    with pytest.raises(ValueError, match="reserved"):
        GrammarBudget(allowed_kinds=frozenset({"terminal", "sequence"}))


def test_splitter_cancellation_is_traced_before_terminal_commit() -> None:
    cancelled = False
    expected = (evidence("a", "A"),)
    root = work("root", (0,), ("a",))

    class CancellingSplitter:
        def split(self, node: WorkNode[str], context: object) -> object:
            nonlocal cancelled
            del node, context
            cancelled = True
            return Terminal(expected, "too-late")

    runner = RecursiveWhileReducer(
        splitter=CancellingSplitter(),
        grammar=BoundedGrammar(),
        cancelled=lambda: cancelled,
    )
    outcome = runner.run(root, expected_evidence=expected)

    assert outcome.status is RunStatus.CANCELLED
    assert outcome.root is None
    assert outcome.unresolved_node_ids == ("root",)
    assert [event.event for event in outcome.trace][-1] == "CANCEL"


def test_entry_cancellation_returns_cancelled_without_calling_splitter() -> None:
    expected = (evidence("a", "A"),)
    root = work("root", (0,), ("a",))

    class ForbiddenSplitter:
        def split(self, node: WorkNode[str], context: object) -> object:
            del node, context
            raise AssertionError("splitter must not run")

    runner = RecursiveWhileReducer(
        splitter=ForbiddenSplitter(),
        grammar=BoundedGrammar(),
        cancelled=lambda: True,
    )
    outcome = runner.run(root, expected_evidence=expected)

    assert outcome.status is RunStatus.CANCELLED
    assert outcome.root is None
    assert outcome.trace[-1].event == "CANCEL"


def test_final_cancellation_suppresses_already_computed_root() -> None:
    checks = 0
    expected = (evidence("a", "A"),)
    root = work("root", (0,), ("a",))

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    runner = RecursiveWhileReducer(
        splitter=DecisionSplitter({"root": Terminal(expected, "leaf")}),
        grammar=BoundedGrammar(),
        cancelled=cancelled,
    )
    outcome = runner.run(root, expected_evidence=expected)

    assert outcome.status is RunStatus.CANCELLED
    assert outcome.root is None
    assert tuple(item.node_id for item in outcome.derivations) == ("root",)
    assert outcome.unresolved_node_ids == ()
    assert outcome.trace[-1].event == "CANCEL"


@pytest.mark.parametrize(
    "limits, message",
    [
        (RunLimits(max_nodes=3, max_depth=1, max_children=1, max_steps=3), "max_children"),
        (RunLimits(max_nodes=2, max_depth=1, max_children=2, max_steps=3), "max_nodes"),
    ],
)
def test_node_and_child_limits_fail_before_queue_growth(limits: RunLimits, message: str) -> None:
    root = work("root", (2,), ())
    left = work("left", (1,), (), order=0)
    right = work("right", (1,), (), order=1)

    with pytest.raises(PipelineLimitError, match=message):
        execute(root, {"root": Expand("pair", (left, right))}, (), limits=limits)


def test_fresh_import_does_not_load_legacy_or_heavy_modules() -> None:
    code = """
import sys
import app.sparse_pipeline.recursive_control
forbidden = ('PIL', 'cv2', 'numpy', 'torch', 'easyocr', 'pytesseract', 'pdf2image')
legacy = ('app.services', 'app.layout')
loaded = tuple(sys.modules)
assert not any(name == root or name.startswith(root + '.') for root in forbidden for name in loaded)
assert not any(name == root or name.startswith(root + '.') for root in legacy for name in loaded)
"""
    subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        cwd=Path(__file__).parents[2],
    )


def test_known_debug_script_runs_and_derives_three_object_kinds(tmp_path: Path) -> None:
    repository_root = Path(__file__).parents[3]
    script = repository_root / "scripts" / "debug" / "debug_recursive_control.py"
    subprocess.run(
        [
            sys.executable,
            str(script),
            "--output",
            str(tmp_path),
            "--run-id",
            "known-tree",
        ],
        check=True,
        cwd=repository_root,
    )
    derivations = (tmp_path / "known-tree" / "00-control" / "derivations.jsonl").read_text(encoding="utf-8")
    assert '"kind":"paragraph"' in derivations
    assert '"kind":"list"' in derivations
    assert '"kind":"table"' in derivations


def test_known_debug_script_publishes_failed_run_before_nonzero_exit(tmp_path: Path) -> None:
    repository_root = Path(__file__).parents[3]
    script = repository_root / "scripts" / "debug" / "debug_recursive_control.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--output",
            str(tmp_path),
            "--run-id",
            "corrupted-tree",
            "--corrupt-sample",
        ],
        check=False,
        cwd=repository_root,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    stage_dir = tmp_path / "corrupted-tree" / "00-control"
    assert (stage_dir / "error.txt").is_file()
    assert "changed expected evidence" in (stage_dir / "error.txt").read_text(encoding="utf-8")
    assert "ERROR" in (stage_dir / "trace.txt").read_text(encoding="utf-8")
