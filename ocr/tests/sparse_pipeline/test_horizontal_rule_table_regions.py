from app.sparse_pipeline.recursive_object_partition import (
    HorizontalRuleTableRegion,
    horizontal_rule_table_regions,
)


def test_regular_full_width_rules_form_one_table_region() -> None:
    regions = horizontal_rule_table_regions(
        rules=(
            (10, 267, 608, 269),
            (10, 303, 608, 305),
            (10, 338, 608, 340),
            (10, 373, 608, 375),
            (10, 409, 608, 411),
            (25, 50, 100, 52),
        ),
        page_bbox=(0, 0, 618, 459),
    )

    assert regions == (
        HorizontalRuleTableRegion(
            bbox=(10, 231, 608, 447),
            rule_boxes=(
                (10, 267, 608, 269),
                (10, 303, 608, 305),
                (10, 338, 608, 340),
                (10, 373, 608, 375),
                (10, 409, 608, 411),
            ),
            cadence=36,
        ),
    )


def test_irregular_rules_do_not_claim_a_table() -> None:
    assert (
        horizontal_rule_table_regions(
            rules=((10, 100, 608, 102), (10, 120, 608, 122), (10, 190, 608, 192)),
            page_bbox=(0, 0, 618, 459),
        )
        == ()
    )
