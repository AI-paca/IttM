"""The current reducer preserves intrinsic membership partitions, not retired polar families."""

from random import Random

import pytest

from app.sparse_pipeline.block_planning import (
    BlockPlanningConfig,
    BlockPlanningInvariantError,
    BlockPlanningLimitError,
    OverlappingBlockPlanner,
)


def reduce(members, *, windows=None, **config):
    sources = tuple(dict.fromkeys(source for block in members for source in block))
    return OverlappingBlockPlanner(BlockPlanningConfig(**config))._sparse_identifying_candidate_indexes(
        members=members,
        scope_ids=("scope-000000",) * len(members),
        windows=windows if windows is not None else ((0, 1, 0, 1),) * len(members),
        source_ids=sources,
    )


def signatures(members, indexes):
    return {
        source: tuple(index for index in indexes if source in members[index]) for block in members for source in block
    }


@pytest.mark.parametrize("seed", range(12))
def test_reduction_preserves_coverage_and_exact_source_equivalence(seed):
    random = Random(seed)
    sources = tuple(f"segment-{index:03}" for index in range(48))
    members = (sources,) + tuple(tuple(source for source in sources if random.randrange(2)) for _ in range(24))
    original = tuple(members)
    indexes, stats = reduce(members)
    before = signatures(members, range(len(members)))
    after = signatures(members, indexes)
    assert members == original
    assert indexes == reduce(members)[0]
    assert all(after.values())
    assert stats["selected_candidates"] == len(indexes)
    for left in sources:
        for right in sources:
            assert (before[left] == before[right]) == (after[left] == after[right])


def test_intrinsically_inseparable_sources_remain_together():
    members = (("a", "b"), ("b", "a"))
    indexes, stats = reduce(members)
    assert len(indexes) == 1
    assert stats["intrinsic_units"] == 1
    assert signatures(members, indexes)["a"] == signatures(members, indexes)["b"]


def test_non_table_blocks_are_preserved_even_if_redundant():
    members = (("header",), ("header", "value"), ("value",))
    indexes, _ = reduce(members, windows=(None, (0, 1, 0, 1), (0, 1, 0, 1)))
    assert 0 in indexes
    assert all(signatures(members, indexes).values())


def test_candidate_limit_fails_before_reduction():
    with pytest.raises(BlockPlanningInvariantError, match="limit"):
        reduce((("a",), ("b",)), max_signature_candidates=1)


def test_work_budget_is_enforced():
    with pytest.raises(BlockPlanningLimitError, match="work limit"):
        reduce((("a", "b"), ("a",), ("b",)), max_signature_candidate_checks=1)
