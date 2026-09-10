import os
from pathlib import Path

import pytest

from app.pipeline_core.native import PIPELINE_CORE_LIBRARY_ENV, native_pipeline_core


def test_native_pipeline_core_exposes_checked_in_abi():
    core = native_pipeline_core()
    if core is None:
        pytest.skip("build pipeline-core before native adapter test")
    configured = os.environ.get(PIPELINE_CORE_LIBRARY_ENV)
    if configured:
        assert core.path == Path(configured).resolve()

    assert core.recipe_mask(1 | 2 | 4) == 1 << 3
    assert core.add_sparse_signal(3, 11) == 14
    assert core.is_isolated_heading(2, 18, 12, True, True) is True
    assert core.is_isolated_heading(1, 2, 2, True, True) is False
    assert core.span_evidence_score(800, 900, 700, 2, 0) == 71_500
    assert core.span_evidence_score(800, 900, 700, 2, 1) == 56_500
    assert core.should_replace_primary(100, 180, 5, 4) is True
    assert core.should_replace_primary(100, 180, 5, 3) is False
    assert core.should_drop_text_block(100, 100, 17, 20, 20, 0) is True
    assert core.should_drop_text_block(100, 100, 16, 20, 20, 0) is False
    with pytest.raises(ValueError, match="non-negative"):
        core.span_evidence_score(-1, 900, 700, 2, 0)
    with pytest.raises(ValueError, match="non-negative"):
        core.should_replace_primary(-1, 180, 5, 4)
    with pytest.raises(ValueError, match="non-negative"):
        core.should_drop_text_block(100, 100, -1, 20, 20, 0)
    with pytest.raises(ValueError, match="Unknown sparse signal"):
        core.add_sparse_signal(1, 1)
    with pytest.raises(ValueError, match="Unknown sparse code"):
        core.add_sparse_signal(1, 3)
