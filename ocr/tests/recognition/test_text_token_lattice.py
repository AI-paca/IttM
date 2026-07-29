from app.recognition.text_token_lattice import repair_text_from_aligned_word_candidates


def _word(text, confidence):
    return {"text": text, "bbox": (100, 10, 220, 50), "conf": confidence}


def test_token_patch_preserves_line_and_selects_observed_aligned_word():
    primary = "SAMPLE hard OCR table pycc-like + English"
    repaired, decisions = repair_text_from_aligned_word_candidates(
        primary,
        (
            ("eng", [_word("pycc-like", 10)]),
            ("chi_sim", [_word("pycc-like", 0)]),
            ("rus", [_word("русский", 96)]),
        ),
    )

    assert repaired == "SAMPLE hard OCR table русский + English"
    assert decisions[0].primary == "pycc-like"
    assert decisions[0].selected == "русский"


def test_token_patch_rejects_unobserved_primary():
    primary = "keep original line"
    repaired, decisions = repair_text_from_aligned_word_candidates(
        primary,
        (
            ("eng", [_word("different", 70)]),
            ("rus", [_word("другой", 96)]),
        ),
    )

    assert repaired == primary
    assert decisions == ()
