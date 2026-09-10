import numpy as np

from app.sparse_pipeline.adaptive_language_ocr import (
    _compact_internal_whitespace,
)


def test_compacts_long_internal_blank_band_without_losing_ink() -> None:
    pixels = np.full((18, 240, 3), 255, dtype=np.uint8)
    pixels[4:14, 4:34] = 0
    pixels[4:14, 206:236] = 0
    ink = np.any(pixels < 128, axis=2)

    compacted = _compact_internal_whitespace(pixels, ink)

    assert compacted.shape[0] == pixels.shape[0]
    assert compacted.shape[1] < 100
    assert np.count_nonzero(compacted < 128) == np.count_nonzero(pixels < 128)


def test_preserves_normal_word_spacing() -> None:
    pixels = np.full((18, 70, 3), 255, dtype=np.uint8)
    pixels[4:14, 4:24] = 0
    pixels[4:14, 32:52] = 0
    ink = np.any(pixels < 128, axis=2)

    compacted = _compact_internal_whitespace(pixels, ink)

    assert compacted.shape == pixels.shape
