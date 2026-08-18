from app.sparse_pipeline.recursive_object_partition import (
    PartitionNode,
    PartitionSegment,
    StructuralRule,
    partition_recursive_objects,
)


def test_fragmented_rule_grid_promotes_multiline_flow_to_table() -> None:
    segments = tuple(
        PartitionSegment(
            f"row-{index}",
            (5, 8 + index * 15, 95, 16 + index * 15),
            ("geo-root",),
        )
        for index in range(5)
    )
    nodes = (
        PartitionNode(
            "geo-root",
            (0, 0, 100, 90),
            None,
            (),
            tuple(item.segment_id for item in segments),
        ),
    )
    rules = (
        *(StructuralRule("horizontal", (2, y, 98, y + 1)) for y in (5, 20, 35, 50)),
        *(StructuralRule("vertical", (x, 2, x + 1, 82)) for x in (6, 35, 65, 94)),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=(),
        networks=(),
        structural_rules=rules,
    )

    assert len(result) == 1
    assert result[0].kind == "table"
    assert "stable-fragmented-rule-grid" in result[0].evidence


def test_parallel_text_rules_without_crossing_grid_remain_flow() -> None:
    segments = (
        PartitionSegment("paragraph", (5, 8, 95, 70), ("geo-root",)),
    )
    nodes = (
        PartitionNode("geo-root", (0, 0, 100, 90), None, (), ("paragraph",)),
    )

    result = partition_recursive_objects(
        segments=segments,
        nodes=nodes,
        rows=(),
        networks=(),
        structural_rules=tuple(
            StructuralRule("horizontal", (2, y, 98, y + 1))
            for y in (5, 20, 35, 50)
        ),
    )

    assert len(result) == 1
    assert result[0].kind != "table"
