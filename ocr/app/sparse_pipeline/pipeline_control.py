from __future__ import annotations

from app.sparse_pipeline.bounded_grammar import BoundedGrammar
from app.sparse_pipeline.recursive_control import (
    EvidenceAtom,
    Expand,
    RecursiveWhileReducer,
    RunLimits,
    RunOutcome,
    RunStatus,
    Terminal,
    WorkNode,
)

PIPELINE_ORDER = (3, 1, 6, 4, 5, 2, 7)

_STAGE_NAMES = {
    3: "recursive-control",
    1: "geometry-sparse-matrix",
    6: "object-reconstruction",
    4: "page-enhancement-candidate",
    5: "overlapping-context-blocks",
    2: "ocr-evidence-fusion",
    7: "document-assembly",
}


class _PipelineControlSplitter:
    """A non-empty bounded control program for the sparse execution order."""

    def __init__(self) -> None:
        self.atoms = tuple(
            EvidenceAtom(
                atom_id=f"pipeline-stage-{stage:02d}",
                source_id="sparse-pipeline-order",
                order_key=(index,),
                payload=f"{stage}:{_STAGE_NAMES[stage]}",
            )
            for index, stage in enumerate(PIPELINE_ORDER)
        )
        atom_ids = tuple(atom.atom_id for atom in self.atoms)
        self.stage_nodes = tuple(
            WorkNode(
                node_id=f"control-stage-{stage:02d}",
                payload=atom.payload,
                rank=(1,),
                scope_id="sparse-pipeline",
                order_key=(index,),
                coverage_atom_ids=(atom.atom_id,),
            )
            for index, (stage, atom) in enumerate(zip(PIPELINE_ORDER, self.atoms))
        )
        first = self.stage_nodes[:3]
        second = self.stage_nodes[3:]
        self.groups = (
            WorkNode(
                node_id="control-structural-front",
                payload="control+geometry+objects",
                rank=(2,),
                scope_id="sparse-pipeline",
                order_key=(0,),
                coverage_atom_ids=tuple(
                    atom_id for node in first for atom_id in node.coverage_atom_ids
                ),
            ),
            WorkNode(
                node_id="control-context-back",
                payload="enhance+blocks+ocr+assembly",
                rank=(2,),
                scope_id="sparse-pipeline",
                order_key=(1,),
                coverage_atom_ids=tuple(
                    atom_id for node in second for atom_id in node.coverage_atom_ids
                ),
            ),
        )
        self.root = WorkNode(
            node_id="control-document",
            payload="sparse-document-pipeline",
            rank=(3,),
            scope_id="sparse-pipeline",
            order_key=(0,),
            coverage_atom_ids=atom_ids,
        )

    def split(self, node: WorkNode[str], _context: object) -> object:
        if node.node_id == self.root.node_id:
            return Expand("pipeline-halves", self.groups)
        if node.node_id == self.groups[0].node_id:
            return Expand("structural-stages", self.stage_nodes[:3])
        if node.node_id == self.groups[1].node_id:
            return Expand("context-stages", self.stage_nodes[3:])
        for stage_node, atom in zip(self.stage_nodes, self.atoms):
            if node.node_id == stage_node.node_id:
                return Terminal((atom,), "scheduled-stage")
        raise ValueError(f"unknown pipeline control node: {node.node_id}")


def run_pipeline_control() -> RunOutcome:
    """Execute and validate the real, non-empty sparse stage schedule."""

    program = _PipelineControlSplitter()
    outcome = RecursiveWhileReducer(
        splitter=program,
        grammar=BoundedGrammar(),
        limits=RunLimits(
            max_nodes=16,
            max_depth=3,
            max_children=7,
            max_steps=32,
        ),
    ).run(program.root, expected_evidence=program.atoms)
    if outcome.status is not RunStatus.COMPLETE or outcome.root is None:
        raise RuntimeError("sparse pipeline control did not complete")
    if tuple(atom.payload for atom in outcome.evidence) != tuple(
        f"{stage}:{_STAGE_NAMES[stage]}" for stage in PIPELINE_ORDER
    ):
        raise RuntimeError("sparse pipeline control reordered stage evidence")
    return outcome


__all__ = ["PIPELINE_ORDER", "run_pipeline_control"]
