from __future__ import annotations

from app.sparse_pipeline.pipeline_control import PIPELINE_ORDER, run_pipeline_control
from app.sparse_pipeline.recursive_control import RunStatus


def test_pipeline_control_is_nonempty_recursive_and_exactly_ordered() -> None:
    outcome = run_pipeline_control()

    assert PIPELINE_ORDER == (3, 1, 6, 4, 5, 2, 7)
    assert outcome.status is RunStatus.COMPLETE
    assert outcome.root is not None
    assert len(outcome.evidence) == len(PIPELINE_ORDER)
    assert tuple(int(atom.payload.split(":", 1)[0]) for atom in outcome.evidence) == (PIPELINE_ORDER)
    assert {event.event for event in outcome.trace} >= {
        "ENTER",
        "EXPAND",
        "TERMINAL",
        "REDUCE",
        "EXIT",
    }
    assert len(outcome.derivations) == 10
