from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from app.sparse_pipeline.recursive_control import (
    Derivation,
    GrammarContext,
    ReductionProposal,
    WorkNode,
)

PayloadT = TypeVar("PayloadT")
AtomT = TypeVar("AtomT", bound=str)


@dataclass(frozen=True)
class GrammarDecision:
    rule_id: str
    kind: str
    attributes: tuple[tuple[str, str], ...] = ()


GrammarClassifier = Callable[
    [WorkNode[PayloadT], tuple[Derivation, ...], GrammarContext[AtomT]],
    GrammarDecision,
]


class BoundedGrammar(Generic[PayloadT, AtomT]):
    """A grammar that can classify structure but cannot rewrite evidence.

    The classifier only chooses metadata. Child references are always copied in
    canonical order here; atom provenance is assembled by the controller.
    """

    def __init__(self, classifier: GrammarClassifier[PayloadT, AtomT] | None = None) -> None:
        self._classifier = classifier or self._neutral_decision

    def reduce(
        self,
        node: WorkNode[PayloadT],
        children: tuple[Derivation, ...],
        context: GrammarContext[AtomT],
    ) -> ReductionProposal:
        context.raise_if_cancelled()
        budget = context.budget
        child_result_ids = tuple(child.result_id for child in children)
        atom_count = sum(len(child.atom_ids) for child in children)

        if any(child.fallback for child in children):
            return ReductionProposal(
                rule_id="fallback:child-fallback",
                kind=budget.neutral_kind,
                child_result_ids=child_result_ids,
                fallback_reason="child-fallback",
            )

        if len(children) > budget.max_direct_children or atom_count > budget.max_atoms:
            return ReductionProposal(
                rule_id="fallback:grammar-budget",
                kind=budget.neutral_kind,
                child_result_ids=child_result_ids,
                fallback_reason="grammar-budget",
            )

        scope_mismatch = any(scope_id != node.scope_id for child in children for scope_id in child.evidence_scope_ids)
        if scope_mismatch:
            return ReductionProposal(
                rule_id="grammar:scope-boundary",
                kind=budget.neutral_kind,
                child_result_ids=child_result_ids,
            )

        decision = self._classifier(node, children, context)
        context.raise_if_cancelled()
        attributes_allowed = set(decision.attributes).issubset(budget.allowed_attributes)
        if decision.kind not in budget.allowed_kinds or not attributes_allowed:
            return ReductionProposal(
                rule_id="fallback:scope-kind-or-attribute",
                kind=budget.neutral_kind,
                child_result_ids=child_result_ids,
                fallback_reason="scope-kind-or-attribute",
            )

        return ReductionProposal(
            rule_id=decision.rule_id,
            kind=decision.kind,
            child_result_ids=child_result_ids,
            attributes=decision.attributes,
        )

    @staticmethod
    def _neutral_decision(
        node: WorkNode[PayloadT],
        children: tuple[Derivation, ...],
        context: GrammarContext[AtomT],
    ) -> GrammarDecision:
        del node, children, context
        return GrammarDecision(rule_id="grammar:sequence", kind="sequence")
