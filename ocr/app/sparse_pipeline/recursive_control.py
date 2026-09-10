from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Callable, Generic, Mapping, Protocol, TypeVar, Union

PayloadT = TypeVar("PayloadT")
AtomT = TypeVar("AtomT", bound=str)
CancellationCheck = Callable[[], bool]


def _validate_non_empty_string(name: str, value: object) -> None:
    if type(value) is not str or not value:
        raise TypeError(f"{name} must be a non-empty string")


def _validate_string_tuple(name: str, values: object, *, allow_empty: bool) -> None:
    if type(values) is not tuple:
        raise TypeError(f"{name} must be an immutable tuple of strings")
    if not allow_empty and not values:
        raise ValueError(f"{name} must not be empty")
    if any(type(value) is not str or not value for value in values):
        raise TypeError(f"{name} must contain only non-empty strings")


def _validate_string_pairs(name: str, values: object, *, frozen: bool) -> None:
    expected_type = frozenset if frozen else tuple
    if type(values) is not expected_type:
        container = "frozenset" if frozen else "tuple"
        raise TypeError(f"{name} must be an immutable {container} of string pairs")
    for pair in values:
        if type(pair) is not tuple or len(pair) != 2:
            raise TypeError(f"{name} must contain only two-item tuples")
        if any(type(value) is not str or not value for value in pair):
            raise TypeError(f"{name} pairs must contain only non-empty strings")


def _validate_index_tuple(name: str, values: object) -> None:
    """Require an immutable, non-empty tuple of real non-negative integers."""

    if type(values) is not tuple or not values:
        raise TypeError(f"{name} must be a non-empty tuple of non-negative integers")
    if any(type(value) is not int for value in values):
        raise TypeError(f"{name} must contain only non-negative integers")
    if any(value < 0 for value in values):
        raise ValueError(f"{name} must contain only non-negative integers")


class PipelineInvariantError(RuntimeError):
    """Raised when a stage violates a lossless-control invariant."""


class PipelineLimitError(RuntimeError):
    """Raised when the bounded controller cannot safely continue."""


class PipelineCancelled(RuntimeError):
    """Raised cooperatively by a bounded callback after cancellation."""


class FailureMode(str, Enum):
    STRICT = "strict"
    RECOVER = "recover"


class RunStatus(str, Enum):
    COMPLETE = "complete"
    DEGRADED = "degraded"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class WorkNode(Generic[PayloadT]):
    node_id: str
    payload: PayloadT
    rank: tuple[int, ...]
    scope_id: str
    order_key: tuple[int, ...]
    coverage_atom_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_non_empty_string("node_id", self.node_id)
        _validate_non_empty_string("scope_id", self.scope_id)
        _validate_index_tuple("rank", self.rank)
        _validate_index_tuple("order_key", self.order_key)
        _validate_string_tuple("coverage_atom_ids", self.coverage_atom_ids, allow_empty=True)
        if len(self.coverage_atom_ids) != len(set(self.coverage_atom_ids)):
            raise ValueError("coverage_atom_ids must be unique")


@dataclass(frozen=True)
class EvidenceAtom(Generic[AtomT]):
    atom_id: str
    payload: AtomT
    order_key: tuple[int, ...]
    source_id: str

    def __post_init__(self) -> None:
        _validate_non_empty_string("atom_id", self.atom_id)
        _validate_non_empty_string("source_id", self.source_id)
        _validate_index_tuple("order_key", self.order_key)
        if type(self.payload) is not str:
            raise TypeError("evidence payload must be an immutable string")


@dataclass(frozen=True)
class EvidenceView(Generic[AtomT]):
    _registry: Mapping[str, EvidenceAtom[AtomT]] = field(repr=False, compare=False)
    atom_ids: tuple[str, ...]
    _allowed_atom_ids: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_string_tuple("atom_ids", self.atom_ids, allow_empty=True)
        object.__setattr__(self, "_allowed_atom_ids", frozenset(self.atom_ids))
        unknown = set(self.atom_ids).difference(self._registry)
        if unknown:
            raise PipelineInvariantError(f"evidence view contains unknown atom IDs: {','.join(sorted(unknown))}")

    @property
    def atoms(self) -> tuple[EvidenceAtom[AtomT], ...]:
        return tuple(self._registry[atom_id] for atom_id in self.atom_ids)

    def select(self, atom_ids: tuple[str, ...]) -> tuple[EvidenceAtom[AtomT], ...]:
        foreign = set(atom_ids).difference(self._allowed_atom_ids)
        if foreign:
            raise PipelineInvariantError(
                f"grammar requested out-of-scope evidence atom(s): {','.join(sorted(foreign))}"
            )
        return tuple(self._registry[atom_id] for atom_id in atom_ids)


@dataclass(frozen=True)
class Terminal(Generic[AtomT]):
    atoms: tuple[EvidenceAtom[AtomT], ...]
    reason: str
    fallback: bool = False

    def __post_init__(self) -> None:
        if type(self.atoms) is not tuple:
            raise TypeError("terminal atoms must be an immutable tuple")
        _validate_non_empty_string("terminal reason", self.reason)
        if type(self.fallback) is not bool:
            raise TypeError("terminal fallback must be a boolean")


@dataclass(frozen=True)
class Expand(Generic[PayloadT]):
    split_rule: str
    children: tuple[WorkNode[PayloadT], ...]

    def __post_init__(self) -> None:
        _validate_non_empty_string("split_rule", self.split_rule)
        if type(self.children) is not tuple or any(type(child) is not WorkNode for child in self.children):
            raise TypeError("children must be an immutable tuple of WorkNode values")


SplitDecision = Union[Terminal[AtomT], Expand[PayloadT]]


@dataclass(frozen=True)
class SplitContext:
    depth: int
    nodes_seen: int
    steps: int
    cancelled: CancellationCheck

    def raise_if_cancelled(self) -> None:
        if self.cancelled():
            raise PipelineCancelled("split cancelled")


@dataclass(frozen=True)
class RunLimits:
    max_nodes: int = 10_000
    max_depth: int = 256
    max_children: int = 256
    max_steps: int = 20_000

    def __post_init__(self) -> None:
        if any(type(value) is not int for value in (self.max_nodes, self.max_depth, self.max_children, self.max_steps)):
            raise TypeError("run limits must be integers")
        if min(self.max_nodes, self.max_children, self.max_steps) < 1:
            raise ValueError("node, child, and step limits must be positive")
        if self.max_depth < 0:
            raise ValueError("max_depth must be non-negative")


@dataclass(frozen=True)
class GrammarBudget:
    max_direct_children: int = 256
    max_atoms: int = 100_000
    allowed_kinds: frozenset[str] = field(
        default_factory=lambda: frozenset({"sequence", "container", "paragraph", "list", "table"})
    )
    neutral_kind: str = "sequence"
    allowed_attributes: frozenset[tuple[str, str]] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if type(self.max_direct_children) is not int or type(self.max_atoms) is not int:
            raise TypeError("grammar limits must be integers")
        if self.max_direct_children < 1 or self.max_atoms < 1:
            raise ValueError("grammar limits must be positive")
        if type(self.allowed_kinds) is not frozenset or any(
            type(kind) is not str or not kind for kind in self.allowed_kinds
        ):
            raise TypeError("allowed_kinds must be a frozenset of non-empty strings")
        if not self.allowed_kinds:
            raise ValueError("allowed_kinds must not be empty")
        _validate_non_empty_string("neutral_kind", self.neutral_kind)
        _validate_string_pairs("allowed_attributes", self.allowed_attributes, frozen=True)
        if self.neutral_kind not in self.allowed_kinds:
            raise ValueError("neutral_kind must be allowed")
        if "terminal" in self.allowed_kinds:
            raise ValueError("terminal kind is reserved for controller-created leaves")


@dataclass(frozen=True)
class GrammarContext(Generic[AtomT]):
    budget: GrammarBudget
    evidence: EvidenceView[AtomT]
    cancelled: CancellationCheck

    def raise_if_cancelled(self) -> None:
        if self.cancelled():
            raise PipelineCancelled("grammar cancelled")


@dataclass(frozen=True)
class ReductionProposal:
    rule_id: str
    kind: str
    child_result_ids: tuple[str, ...]
    attributes: tuple[tuple[str, str], ...] = ()
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        _validate_non_empty_string("rule_id", self.rule_id)
        _validate_non_empty_string("kind", self.kind)
        _validate_string_tuple("child_result_ids", self.child_result_ids, allow_empty=True)
        _validate_string_pairs("attributes", self.attributes, frozen=False)
        if self.fallback_reason is not None:
            _validate_non_empty_string("fallback_reason", self.fallback_reason)


@dataclass(frozen=True)
class Derivation:
    result_id: str
    node_id: str
    scope_id: str
    evidence_scope_ids: tuple[str, ...]
    kind: str
    child_result_ids: tuple[str, ...]
    atom_ids: tuple[str, ...]
    attributes: tuple[tuple[str, str], ...]
    rule_id: str
    fallback: bool = False


@dataclass(frozen=True)
class TraceEvent:
    step: int
    event: str
    node_id: str
    details: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RunOutcome(Generic[AtomT]):
    status: RunStatus
    root: Derivation | None
    derivations: tuple[Derivation, ...]
    evidence: tuple[EvidenceAtom[AtomT], ...]
    trace: tuple[TraceEvent, ...]
    unresolved_node_ids: tuple[str, ...]
    steps: int
    error: str | None = None


class Splitter(Protocol[PayloadT, AtomT]):
    def split(self, node: WorkNode[PayloadT], context: SplitContext) -> SplitDecision: ...


class ReducerGrammar(Protocol[PayloadT, AtomT]):
    def reduce(
        self,
        node: WorkNode[PayloadT],
        children: tuple[Derivation, ...],
        context: GrammarContext[AtomT],
    ) -> ReductionProposal: ...


OpaqueLeafFactory = Callable[[WorkNode[PayloadT], str], Terminal[AtomT]]


@dataclass
class _Frame(Generic[PayloadT]):
    node: WorkNode[PayloadT]
    depth: int
    entered: bool = False
    expanded: bool = False
    child_ids: tuple[str, ...] = ()


@dataclass
class _ExecutionState(Generic[PayloadT, AtomT]):
    stack: list[_Frame[PayloadT]]
    evidence_by_id: dict[str, EvidenceAtom[AtomT]] = field(default_factory=dict)
    completion_order: list[Derivation] = field(default_factory=list)
    trace: list[TraceEvent] = field(default_factory=list)
    steps: int = 0


class RecursiveWhileReducer(Generic[PayloadT, AtomT]):
    """Lossless post-order reducer driven by one explicit ``while`` stack.

    Splitting and grammar reduction deliberately live in the same control
    loop. No image, matrix, OCR, or document serializer is reachable here.
    """

    def __init__(
        self,
        *,
        splitter: Splitter[PayloadT, AtomT],
        grammar: ReducerGrammar[PayloadT, AtomT],
        limits: RunLimits | None = None,
        grammar_budget: GrammarBudget | None = None,
        failure_mode: FailureMode | str = FailureMode.STRICT,
        opaque_leaf_factory: OpaqueLeafFactory[PayloadT, AtomT] | None = None,
        cancelled: CancellationCheck | None = None,
    ) -> None:
        self._splitter = splitter
        self._grammar = grammar
        self._limits = limits or RunLimits()
        self._grammar_budget = grammar_budget or GrammarBudget()
        self._failure_mode = FailureMode(failure_mode)
        self._opaque_leaf_factory = opaque_leaf_factory
        self._cancelled = cancelled or (lambda: False)

        if self._failure_mode is FailureMode.RECOVER and opaque_leaf_factory is None:
            raise ValueError("recover mode requires opaque_leaf_factory")

    def run(
        self,
        root: WorkNode[PayloadT],
        *,
        expected_evidence: tuple[EvidenceAtom[AtomT], ...],
    ) -> RunOutcome:
        if type(root) is not WorkNode:
            raise TypeError("root must be a WorkNode")
        state = _ExecutionState[PayloadT, AtomT](stack=[_Frame(node=root, depth=0)])
        try:
            if type(expected_evidence) is not tuple or any(
                type(atom) is not EvidenceAtom for atom in expected_evidence
            ):
                raise TypeError("expected_evidence must be an immutable tuple of EvidenceAtom values")
            return self._run(root, expected_evidence=expected_evidence, state=state)
        except (KeyboardInterrupt, SystemExit):
            raise
        except PipelineCancelled:
            cancelled_node_id = state.stack[-1].node.node_id if state.stack else root.node_id
            self._trace(
                state.trace,
                state.steps,
                "CANCEL",
                cancelled_node_id,
                unresolved=len(state.stack),
            )
            return RunOutcome(
                status=RunStatus.CANCELLED,
                root=None,
                derivations=tuple(state.completion_order),
                evidence=tuple(state.evidence_by_id.values()),
                trace=tuple(state.trace),
                unresolved_node_ids=tuple(frame.node.node_id for frame in state.stack),
                steps=state.steps,
            )
        except Exception as exc:
            failure_node_id = state.stack[-1].node.node_id if state.stack else root.node_id
            self._trace(
                state.trace,
                state.steps,
                "ERROR",
                failure_node_id,
                error_type=type(exc).__name__,
                message=str(exc),
            )
            outcome = RunOutcome(
                status=RunStatus.FAILED,
                root=None,
                derivations=tuple(state.completion_order),
                evidence=tuple(state.evidence_by_id.values()),
                trace=tuple(state.trace),
                unresolved_node_ids=tuple(frame.node.node_id for frame in state.stack),
                steps=state.steps,
                error=f"{type(exc).__name__}: {exc}",
            )
            setattr(exc, "outcome", outcome)
            raise

    def _run(
        self,
        root: WorkNode[PayloadT],
        *,
        expected_evidence: tuple[EvidenceAtom[AtomT], ...],
        state: _ExecutionState[PayloadT, AtomT],
    ) -> RunOutcome:
        stack = state.stack
        evidence_by_id = state.evidence_by_id
        completion_order = state.completion_order
        trace = state.trace
        steps = state.steps

        expected_atom_ids = tuple(atom.atom_id for atom in expected_evidence)
        if expected_atom_ids != root.coverage_atom_ids:
            raise PipelineInvariantError(
                f"expected evidence does not match root coverage: "
                f"{expected_atom_ids!r} != {root.coverage_atom_ids!r}"
            )
        if len(expected_atom_ids) != len(set(expected_atom_ids)):
            raise PipelineInvariantError("expected evidence atom IDs must be unique")
        expected_order_keys = tuple(atom.order_key for atom in expected_evidence)
        if len(expected_order_keys) != len(set(expected_order_keys)):
            raise PipelineInvariantError("expected evidence order keys must be unique")
        if expected_order_keys != tuple(sorted(expected_order_keys)):
            raise PipelineInvariantError("expected evidence is not in canonical evidence order")
        expected_by_id = {atom.atom_id: atom for atom in expected_evidence}
        expected_registry = MappingProxyType(expected_by_id)

        seen_node_ids = {root.node_id}
        results: dict[str, Derivation] = {}

        while stack:
            if self._cancelled():
                self._trace(
                    trace,
                    steps,
                    "CANCEL",
                    stack[-1].node.node_id,
                    unresolved=len(stack),
                )
                return RunOutcome(
                    status=RunStatus.CANCELLED,
                    root=None,
                    derivations=tuple(completion_order),
                    evidence=tuple(evidence_by_id.values()),
                    trace=tuple(trace),
                    unresolved_node_ids=tuple(frame.node.node_id for frame in stack),
                    steps=steps,
                )

            steps += 1
            state.steps = steps
            if steps > self._limits.max_steps:
                raise PipelineLimitError(f"max_steps exceeded at {self._limits.max_steps}")

            frame = stack[-1]
            if not frame.entered:
                frame.entered = True
                self._trace(trace, steps, "ENTER", frame.node.node_id, depth=frame.depth)
            if not frame.expanded:
                context = SplitContext(
                    depth=frame.depth,
                    nodes_seen=len(seen_node_ids),
                    steps=steps,
                    cancelled=self._cancelled,
                )
                recovered_split = False
                try:
                    decision = self._splitter.split(frame.node, context)
                    context.raise_if_cancelled()
                    self._validate_decision(
                        parent=frame,
                        decision=decision,
                        seen_node_ids=seen_node_ids,
                    )
                except Exception as exc:
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    if isinstance(exc, PipelineCancelled):
                        self._trace(trace, steps, "CANCEL", frame.node.node_id, unresolved=len(stack))
                        return RunOutcome(
                            status=RunStatus.CANCELLED,
                            root=None,
                            derivations=tuple(completion_order),
                            evidence=tuple(evidence_by_id.values()),
                            trace=tuple(trace),
                            unresolved_node_ids=tuple(item.node.node_id for item in stack),
                            steps=steps,
                        )
                    if self._failure_mode is FailureMode.STRICT:
                        if isinstance(exc, (PipelineInvariantError, PipelineLimitError)):
                            raise
                        raise PipelineInvariantError(
                            f"splitter failed for {frame.node.node_id}: {type(exc).__name__}: {exc}"
                        ) from exc
                    decision = self._recover_terminal(frame.node, f"split:{type(exc).__name__}:{exc}")
                    recovered_split = True
                    self._trace(trace, steps, "FALLBACK", frame.node.node_id, reason=decision.reason)

                if isinstance(decision, Terminal):
                    if decision.fallback and not recovered_split:
                        self._trace(
                            trace,
                            steps,
                            "FALLBACK",
                            frame.node.node_id,
                            reason=decision.reason,
                        )
                    try:
                        derivation = self._terminal_derivation(
                            frame.node,
                            decision,
                            expected_by_id,
                            evidence_by_id,
                            fallback=decision.fallback,
                        )
                    except Exception as exc:
                        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                            raise
                        if self._failure_mode is FailureMode.STRICT or recovered_split:
                            if isinstance(exc, PipelineInvariantError):
                                raise
                            raise PipelineInvariantError(
                                f"terminal failed for {frame.node.node_id}: {type(exc).__name__}: {exc}"
                            ) from exc
                        decision = self._recover_terminal(
                            frame.node,
                            f"terminal:{type(exc).__name__}:{exc}",
                        )
                        self._trace(
                            trace,
                            steps,
                            "FALLBACK",
                            frame.node.node_id,
                            reason=decision.reason,
                        )
                        derivation = self._terminal_derivation(
                            frame.node,
                            decision,
                            expected_by_id,
                            evidence_by_id,
                            fallback=True,
                        )
                    results[frame.node.node_id] = derivation
                    completion_order.append(derivation)
                    self._trace(trace, steps, "TERMINAL", frame.node.node_id, reason=decision.reason)
                    self._trace(trace, steps, "EXIT", frame.node.node_id, atoms=len(derivation.atom_ids))
                    stack.pop()
                    continue

                frame.expanded = True
                frame.child_ids = tuple(child.node_id for child in decision.children)
                seen_node_ids.update(frame.child_ids)
                self._trace(
                    trace,
                    steps,
                    "EXPAND",
                    frame.node.node_id,
                    rule=decision.split_rule,
                    children=",".join(frame.child_ids),
                )
                for child in reversed(decision.children):
                    stack.append(_Frame(node=child, depth=frame.depth + 1))
                continue

            children = tuple(results[child_id] for child_id in frame.child_ids)
            try:
                derivation = self._reduce(
                    frame.node,
                    children,
                    expected_registry,
                    trace,
                    steps,
                )
            except PipelineCancelled:
                self._trace(trace, steps, "CANCEL", frame.node.node_id, unresolved=len(stack))
                return RunOutcome(
                    status=RunStatus.CANCELLED,
                    root=None,
                    derivations=tuple(completion_order),
                    evidence=tuple(evidence_by_id.values()),
                    trace=tuple(trace),
                    unresolved_node_ids=tuple(item.node.node_id for item in stack),
                    steps=steps,
                )
            results[frame.node.node_id] = derivation
            completion_order.append(derivation)
            self._trace(
                trace,
                steps,
                "REDUCE",
                frame.node.node_id,
                rule=derivation.rule_id,
                children=",".join(derivation.child_result_ids),
            )
            self._trace(trace, steps, "EXIT", frame.node.node_id, atoms=len(derivation.atom_ids))
            stack.pop()

        if self._cancelled():
            self._trace(trace, steps, "CANCEL", root.node_id, unresolved=0)
            return RunOutcome(
                status=RunStatus.CANCELLED,
                root=None,
                derivations=tuple(completion_order),
                evidence=tuple(evidence_by_id.values()),
                trace=tuple(trace),
                unresolved_node_ids=(),
                steps=steps,
            )

        root_derivation = results[root.node_id]
        if root_derivation.atom_ids != root.coverage_atom_ids:
            raise PipelineInvariantError("completed root does not exactly cover its declared evidence")
        return RunOutcome(
            status=RunStatus.DEGRADED if root_derivation.fallback else RunStatus.COMPLETE,
            root=root_derivation,
            derivations=tuple(completion_order),
            evidence=tuple(evidence_by_id.values()),
            trace=tuple(trace),
            unresolved_node_ids=(),
            steps=steps,
        )

    def _validate_decision(
        self,
        *,
        parent: _Frame[PayloadT],
        decision: SplitDecision,
        seen_node_ids: set[str],
    ) -> None:
        if isinstance(decision, Terminal):
            return
        if not isinstance(decision, Expand):
            raise PipelineInvariantError(f"unknown split decision: {type(decision).__name__}")
        if not decision.children:
            raise PipelineInvariantError("an expansion must contain at least one child")
        if len(decision.children) > self._limits.max_children:
            raise PipelineLimitError(f"max_children exceeded at node {parent.node.node_id}")
        if parent.depth + 1 > self._limits.max_depth:
            raise PipelineLimitError(f"max_depth exceeded at node {parent.node.node_id}")
        if len(seen_node_ids) + len(decision.children) > self._limits.max_nodes:
            raise PipelineLimitError(f"max_nodes exceeded at node {parent.node.node_id}")

        child_ids = tuple(child.node_id for child in decision.children)
        if len(child_ids) != len(set(child_ids)):
            raise PipelineInvariantError(f"duplicate child ID below {parent.node.node_id}")
        duplicated = seen_node_ids.intersection(child_ids)
        if duplicated:
            raise PipelineInvariantError(f"reused node ID(s): {','.join(sorted(duplicated))}")
        for child in decision.children:
            if len(child.rank) != len(parent.node.rank):
                raise PipelineInvariantError(
                    f"child rank arity changed: {child.node_id} {child.rank!r} vs {parent.node.rank!r}"
                )
            if child.rank >= parent.node.rank:
                raise PipelineInvariantError(
                    f"child rank must decrease: {child.node_id} {child.rank!r} >= {parent.node.rank!r}"
                )
        child_order = tuple(child.order_key for child in decision.children)
        if len(child_order) != len(set(child_order)) or child_order != tuple(sorted(child_order)):
            raise PipelineInvariantError(f"children below {parent.node.node_id} must have unique canonical order keys")
        child_coverage = tuple(atom_id for child in decision.children for atom_id in child.coverage_atom_ids)
        if child_coverage != parent.node.coverage_atom_ids:
            raise PipelineInvariantError(
                f"children do not exactly cover {parent.node.node_id}: "
                f"{child_coverage!r} != {parent.node.coverage_atom_ids!r}"
            )

    def _terminal_derivation(
        self,
        node: WorkNode[PayloadT],
        terminal: Terminal[AtomT],
        expected_by_id: dict[str, EvidenceAtom[AtomT]],
        evidence_by_id: dict[str, EvidenceAtom[AtomT]],
        *,
        fallback: bool,
    ) -> Derivation:
        if any(type(atom) is not EvidenceAtom for atom in terminal.atoms):
            raise PipelineInvariantError(f"terminal {node.node_id} contains a non-EvidenceAtom value")
        ordered_atoms = tuple(sorted(terminal.atoms, key=lambda atom: atom.order_key))
        order_keys = tuple(atom.order_key for atom in ordered_atoms)
        if len(order_keys) != len(set(order_keys)):
            raise PipelineInvariantError(f"terminal {node.node_id} contains duplicate evidence order keys")
        atom_ids = tuple(atom.atom_id for atom in ordered_atoms)
        if atom_ids != node.coverage_atom_ids:
            raise PipelineInvariantError(
                f"terminal does not exactly cover {node.node_id}: {atom_ids!r} != {node.coverage_atom_ids!r}"
            )
        if len(atom_ids) != len(set(atom_ids)):
            raise PipelineInvariantError(f"terminal {node.node_id} contains duplicate atom IDs")
        for evidence_atom in ordered_atoms:
            expected = expected_by_id.get(evidence_atom.atom_id)
            if expected is None or evidence_atom != expected:
                raise PipelineInvariantError(f"terminal changed expected evidence atom {evidence_atom.atom_id}")
        duplicated = set(evidence_by_id).intersection(atom_ids)
        if duplicated:
            raise PipelineInvariantError(f"reused atom ID(s): {','.join(sorted(duplicated))}")
        evidence_by_id.update((atom.atom_id, atom) for atom in ordered_atoms)
        return Derivation(
            result_id=self._result_id(node.node_id),
            node_id=node.node_id,
            scope_id=node.scope_id,
            evidence_scope_ids=(node.scope_id,),
            kind="terminal",
            child_result_ids=(),
            atom_ids=atom_ids,
            attributes=(("reason", terminal.reason),),
            rule_id=f"terminal:{terminal.reason}",
            fallback=fallback,
        )

    def _reduce(
        self,
        node: WorkNode[PayloadT],
        children: tuple[Derivation, ...],
        expected_registry: Mapping[str, EvidenceAtom[AtomT]],
        trace: list[TraceEvent],
        steps: int,
    ) -> Derivation:
        try:
            scope_mismatch = any(
                scope_id != node.scope_id for child in children for scope_id in child.evidence_scope_ids
            )
            local_atom_ids = () if scope_mismatch else node.coverage_atom_ids
            context = GrammarContext(
                budget=self._grammar_budget,
                evidence=EvidenceView(expected_registry, local_atom_ids),
                cancelled=self._cancelled,
            )
            context.raise_if_cancelled()
            proposal = self._grammar.reduce(node, children, context)
            context.raise_if_cancelled()
            self._validate_proposal(node, children, proposal)
        except Exception as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, PipelineCancelled):
                raise
            if self._failure_mode is FailureMode.STRICT:
                if isinstance(exc, PipelineInvariantError):
                    raise
                raise PipelineInvariantError(f"grammar failed for {node.node_id}: {type(exc).__name__}: {exc}") from exc
            proposal = self._lossless_fallback(children, f"grammar:{type(exc).__name__}:{exc}")

        if proposal.fallback_reason is not None:
            self._trace(trace, steps, "FALLBACK", node.node_id, reason=proposal.fallback_reason)

        atom_ids = tuple(atom_id for child in children for atom_id in child.atom_ids)
        evidence_scope_ids = tuple(
            dict.fromkeys(scope_id for child in children for scope_id in child.evidence_scope_ids)
        )
        return Derivation(
            result_id=self._result_id(node.node_id),
            node_id=node.node_id,
            scope_id=node.scope_id,
            evidence_scope_ids=evidence_scope_ids,
            kind=proposal.kind,
            child_result_ids=proposal.child_result_ids,
            atom_ids=atom_ids,
            attributes=proposal.attributes,
            rule_id=proposal.rule_id,
            fallback=proposal.fallback_reason is not None or any(child.fallback for child in children),
        )

    def _validate_proposal(
        self,
        node: WorkNode[PayloadT],
        children: tuple[Derivation, ...],
        proposal: ReductionProposal,
    ) -> None:
        if not isinstance(proposal, ReductionProposal):
            raise PipelineInvariantError(f"grammar returned {type(proposal).__name__}, expected ReductionProposal")
        expected = tuple(child.result_id for child in children)
        if proposal.child_result_ids != expected:
            raise PipelineInvariantError(
                f"grammar changed child coverage/order for {node.node_id}: "
                f"{proposal.child_result_ids!r} != {expected!r}"
            )
        if not proposal.rule_id:
            raise PipelineInvariantError("grammar rule_id must not be empty")
        if proposal.kind not in self._grammar_budget.allowed_kinds:
            raise PipelineInvariantError(f"grammar kind is not allowed: {proposal.kind}")
        if proposal.kind == "terminal":
            raise PipelineInvariantError("an internal-node reduction cannot have terminal kind")
        atom_count = sum(len(child.atom_ids) for child in children)
        over_budget = (
            len(children) > self._grammar_budget.max_direct_children or atom_count > self._grammar_budget.max_atoms
        )
        if over_budget and proposal.fallback_reason is None:
            raise PipelineInvariantError("grammar budget exceeded without a lossless fallback")
        if over_budget and proposal.kind != self._grammar_budget.neutral_kind:
            raise PipelineInvariantError("grammar budget fallback must use the neutral kind")
        if proposal.fallback_reason is not None and proposal.kind != self._grammar_budget.neutral_kind:
            raise PipelineInvariantError("every grammar fallback must use the neutral kind")
        if proposal.fallback_reason is not None and proposal.attributes:
            raise PipelineInvariantError("a grammar fallback cannot emit semantic attributes")
        if any(child.fallback for child in children) and proposal.fallback_reason is None:
            raise PipelineInvariantError("a child fallback must propagate as an explicit parent fallback")
        scope_mismatch = any(scope_id != node.scope_id for child in children for scope_id in child.evidence_scope_ids)
        if scope_mismatch and proposal.kind != self._grammar_budget.neutral_kind:
            raise PipelineInvariantError("cross-scope reduction must use the neutral kind")
        attribute_keys = tuple(key for key, _ in proposal.attributes)
        if len(attribute_keys) != len(set(attribute_keys)):
            raise PipelineInvariantError("grammar attributes must have unique keys")
        disallowed_attributes = set(proposal.attributes).difference(self._grammar_budget.allowed_attributes)
        if disallowed_attributes:
            raise PipelineInvariantError(f"grammar attributes are not allowlisted: {sorted(disallowed_attributes)!r}")

    def _recover_terminal(self, node: WorkNode[PayloadT], reason: str) -> Terminal[AtomT]:
        if self._opaque_leaf_factory is None:
            raise PipelineInvariantError("opaque fallback is unavailable")
        if self._cancelled():
            raise PipelineCancelled("recovery cancelled")
        terminal = self._opaque_leaf_factory(node, reason)
        if self._cancelled():
            raise PipelineCancelled("recovery cancelled")
        if not isinstance(terminal, Terminal):
            raise PipelineInvariantError("opaque_leaf_factory must return Terminal")
        return Terminal(atoms=terminal.atoms, reason=f"fallback:{reason}", fallback=True)

    def _lossless_fallback(self, children: tuple[Derivation, ...], reason: str) -> ReductionProposal:
        return ReductionProposal(
            rule_id="fallback:opaque-container",
            kind=self._grammar_budget.neutral_kind,
            child_result_ids=tuple(child.result_id for child in children),
            fallback_reason=reason,
        )

    @staticmethod
    def _result_id(node_id: str) -> str:
        return f"derivation:{node_id}"

    @staticmethod
    def _trace(
        trace: list[TraceEvent],
        step: int,
        event: str,
        node_id: str,
        **details: object,
    ) -> None:
        trace.append(
            TraceEvent(
                step=step,
                event=event,
                node_id=node_id,
                details=tuple((key, str(value)) for key, value in sorted(details.items())),
            )
        )
