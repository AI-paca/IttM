from tests.generated_media import (
    FUNCTIONAL_OCR_FIXTURE_REGISTRY,
    GENERATED_FIXTURE_GENERATOR_VERSION,
    GENERATED_FIXTURE_REGISTRY,
)


EXPECTED_SEEDS = {
    "long-screenshot-receipt": 2026061401,
    "structured-product-table": 2026061402,
    "full-width-banner": 2026061403,
    "mixed-language-card": 2026061404,
}

EXPECTED_GENERATOR_VERSION = "2026.06.14-1"
EXPECTED_FUNCTIONAL_OCR_SEEDS = {
    "generated-simple-paragraph": 2026061451,
    "generated-product-table": 2026061452,
    "generated-low-contrast-noise": 2026061453,
    "generated-small-skew": 2026061454,
}


def test_generated_fixture_ids_are_unique_and_seeds_are_stable():
    ids = [spec.id for spec in GENERATED_FIXTURE_REGISTRY]

    assert len(ids) == len(set(ids))
    assert {spec.id: spec.seed for spec in GENERATED_FIXTURE_REGISTRY} == EXPECTED_SEEDS


def test_generated_fixture_registry_covers_required_categories():
    assert {spec.category for spec in GENERATED_FIXTURE_REGISTRY} >= {
        "long_screenshot",
        "table",
        "banner",
        "mixed_language",
    }


def test_generated_fixture_invariants_are_non_empty_and_pr_safe():
    for spec in GENERATED_FIXTURE_REGISTRY:
        assert spec.tier == "contract"
        assert spec.expected_tokens
        assert all(token.strip() for token in spec.expected_tokens)
        assert "testtables" not in repr(spec).lower()


def test_generated_fixture_generator_version_is_explicit_and_stable():
    assert GENERATED_FIXTURE_GENERATOR_VERSION == EXPECTED_GENERATOR_VERSION
    assert {
        spec.id: spec.generator_version for spec in GENERATED_FIXTURE_REGISTRY
    } == {
        fixture_id: EXPECTED_GENERATOR_VERSION for fixture_id in EXPECTED_SEEDS
    }


def test_functional_ocr_fixture_matrix_is_explicit_and_stable():
    assert {spec.id: spec.seed for spec in FUNCTIONAL_OCR_FIXTURE_REGISTRY} == EXPECTED_FUNCTIONAL_OCR_SEEDS
    assert {spec.category for spec in FUNCTIONAL_OCR_FIXTURE_REGISTRY} == {
        "degraded_text",
        "simple_paragraph",
        "skewed_text",
        "table",
    }
    for spec in FUNCTIONAL_OCR_FIXTURE_REGISTRY:
        assert spec.tier == "quality"
        assert spec.expected_tokens
        assert "testtables" not in repr(spec).lower()
