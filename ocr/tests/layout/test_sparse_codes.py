from PIL import Image, ImageDraw

from app.chunking.vertical import (
    TableCell,
    TableLayout,
    mark_table_empty_slots,
    table_layout_to_rows,
)
from app.layout.sparse_codes import (
    EMPTY_SLOT_CODE,
    MERGE_BOTH_CODES,
    MERGE_LEFT_CODE,
    MERGE_LEFT_CODES,
    MERGE_UP_CODE,
    MERGE_UP_CODES,
    add_sparse_signal,
)
from app.recognition.cell_batches import recognize_missing_table_cell_batches
from app.recognition.segments import (
    TableCellRecognitionCandidate,
    recognize_table_cells,
    should_select_augmented_table_candidate,
    table_cell_candidate_numeric_ratio,
)


def test_sparse_codes_preserve_additive_empty_slot_signal():
    empty_up = add_sparse_signal(MERGE_UP_CODE, EMPTY_SLOT_CODE)
    empty_left = add_sparse_signal(MERGE_LEFT_CODE, EMPTY_SLOT_CODE)
    empty_both = add_sparse_signal(
        MERGE_UP_CODE + MERGE_LEFT_CODE,
        EMPTY_SLOT_CODE,
    )

    assert empty_up == 14
    assert empty_left == 16
    assert empty_both == 19
    assert empty_up in MERGE_UP_CODES
    assert empty_left in MERGE_LEFT_CODES
    assert empty_both in MERGE_BOTH_CODES


def test_layout_phase_marks_blank_slots_without_removing_them():
    image = Image.new("L", (120, 50), "white")
    draw = ImageDraw.Draw(image)
    draw.text((68, 15), "text", fill="black")
    table = TableLayout(
        bbox=(0, 0, 120, 50),
        rows=1,
        cols=2,
        x_lines=(0, 60, 120),
        y_lines=(0, 50),
        cells=(
            TableCell(row=0, col=0, bbox=(0, 0, 60, 50)),
            TableCell(row=0, col=1, bbox=(60, 0, 120, 50)),
        ),
    )

    marked = mark_table_empty_slots(image, table)

    assert len(marked.cells) == 2
    assert marked.cells[0].sparse_code == EMPTY_SLOT_CODE
    assert marked.cells[0].is_empty
    assert marked.cells[1].sparse_code == 0
    assert not marked.cells[1].is_empty


def test_recursive_cell_ocr_skips_uniform_slots_marked_empty_by_layout():
    image = Image.new("L", (120, 50), "white")
    draw = ImageDraw.Draw(image)
    draw.text((68, 15), "text", fill="black")
    table = TableLayout(
        bbox=(0, 0, 120, 50),
        rows=1,
        cols=2,
        x_lines=(0, 60, 120),
        y_lines=(0, 50),
        cells=(
            TableCell(row=0, col=0, bbox=(0, 0, 60, 50)),
            TableCell(row=0, col=1, bbox=(60, 0, 120, 50)),
        ),
    )
    marked = mark_table_empty_slots(image, table)
    calls = 0

    def recognize(_cell_image):
        nonlocal calls
        calls += 1
        return "recognized"

    rows = table_layout_to_rows(
        image,
        marked,
        recognize,
        skip_blank_cells=False,
    )

    assert calls == 1
    assert rows == [["", "recognized"]]


def test_layout_does_not_mark_a_thin_digit_as_empty():
    image = Image.new("L", (120, 50), "white")
    draw = ImageDraw.Draw(image)
    draw.line((25, 12, 25, 28), fill=220, width=1)
    table = TableLayout(
        bbox=(0, 0, 120, 50),
        rows=1,
        cols=2,
        x_lines=(0, 60, 120),
        y_lines=(0, 50),
        cells=(
            TableCell(row=0, col=0, bbox=(0, 0, 60, 50)),
            TableCell(row=0, col=1, bbox=(60, 0, 120, 50)),
        ),
    )
    marked = mark_table_empty_slots(image, table)

    assert not marked.cells[0].is_empty
    assert marked.cells[1].is_empty


def test_batched_cell_ocr_includes_a_thin_digit_candidate():
    image = Image.new("L", (120, 50), "white")
    draw = ImageDraw.Draw(image)
    draw.line((25, 12, 25, 28), fill=220, width=1)
    table = TableLayout(
        bbox=(0, 0, 120, 50),
        rows=1,
        cols=2,
        x_lines=(0, 60, 120),
        y_lines=(0, 50),
        cells=(
            TableCell(row=0, col=0, bbox=(0, 0, 60, 50)),
            TableCell(row=0, col=1, bbox=(60, 0, 120, 50)),
        ),
    )
    marked = mark_table_empty_slots(image, table)

    class FakeEngine:
        def recognize_words(self, _image, psm=6, min_conf=0):
            return [
                {
                    "text": "11",
                    "bbox": (30, 30, 50, 50),
                    "conf": 90,
                }
            ]

    recovered, calls = recognize_missing_table_cell_batches(
        FakeEngine(),
        image,
        marked,
    )

    assert calls == 1
    assert [word["text"] for word in recovered] == ["11"]


def test_batched_cell_ocr_retries_equ_for_numeric_rows():
    image = Image.new("L", (120, 50), "white")
    draw = ImageDraw.Draw(image)
    draw.text((8, 15), "36", fill="black")
    draw.line((85, 12, 85, 28), fill=220, width=1)
    table = TableLayout(
        bbox=(0, 0, 120, 50),
        rows=1,
        cols=2,
        x_lines=(0, 60, 120),
        y_lines=(0, 50),
        cells=(
            TableCell(row=0, col=0, bbox=(0, 0, 60, 50)),
            TableCell(row=0, col=1, bbox=(60, 0, 120, 50)),
        ),
    )
    seed_words = [
        {
            "text": "36",
            "bbox": (5, 10, 35, 35),
            "conf": 90,
        }
    ]

    class FakeEngine:
        def recognize_words(self, _image, psm=6, min_conf=0):
            return [
                {
                    "text": "S",
                    "bbox": (30, 30, 50, 50),
                    "conf": 30,
                }
            ]

        def recognize_words_for_language(
            self,
            _image,
            language,
            psm=6,
            min_conf=0,
        ):
            assert language == "equ"
            return [
                {
                    "text": "5",
                    "bbox": (30, 30, 50, 50),
                    "conf": 80,
                }
            ]

    recovered, calls = recognize_missing_table_cell_batches(
        FakeEngine(),
        image,
        table,
        seed_words=seed_words,
    )

    assert calls == 2
    assert [word["text"] for word in recovered] == ["5"]


def _table_candidate(*texts: str) -> TableCellRecognitionCandidate:
    recovered_words = tuple(
        {
            "text": text,
            "bbox": (index, 0, index + 1, 1),
            "conf": 50,
        }
        for index, text in enumerate(texts)
    )
    return TableCellRecognitionCandidate(
        rows=[],
        calls=1,
        coverage=0.5,
        added_cells=len(recovered_words),
        recovered_words=recovered_words,
    )


def test_wide_grid_rejects_mass_non_numeric_cell_recovery():
    table = TableLayout(
        bbox=(0, 0, 610, 80),
        rows=8,
        cols=61,
        x_lines=(),
        y_lines=(),
        cells=(),
    )
    candidate = _table_candidate(
        "AT",
        "Db",
        "биз:",
        "мекен-жайы",
        "т",
        "=z",
        "п",
        "7",
    )

    selected, reason = should_select_augmented_table_candidate(
        table,
        candidate,
        previous_coverage=0.20,
        augmented_coverage=0.46,
    )

    assert table_cell_candidate_numeric_ratio(candidate) == 0.125
    assert not selected
    assert reason == "wide_non_numeric"


def test_wide_grid_accepts_numeric_cell_recovery():
    table = TableLayout(
        bbox=(0, 0, 600, 200),
        rows=20,
        cols=60,
        x_lines=(),
        y_lines=(),
        cells=(),
    )
    candidate = _table_candidate(
        "Б1.О.01",
        "Математика",
        "36",
        "72",
        "2",
        "4",
        "18",
        "экзамен",
    )

    selected, reason = should_select_augmented_table_candidate(
        table,
        candidate,
        previous_coverage=0.20,
        augmented_coverage=0.40,
    )

    assert table_cell_candidate_numeric_ratio(candidate) == 0.625
    assert selected
    assert reason == "accepted"


def test_regular_text_grid_accepts_non_numeric_cell_recovery():
    table = TableLayout(
        bbox=(0, 0, 200, 200),
        rows=10,
        cols=10,
        x_lines=(),
        y_lines=(),
        cells=(),
    )
    candidate = _table_candidate(
        "Name",
        "Department",
        "Status",
        "Description",
        "Owner",
        "Review",
        "Notes",
        "Active",
    )

    selected, reason = should_select_augmented_table_candidate(
        table,
        candidate,
        previous_coverage=0.20,
        augmented_coverage=0.40,
    )

    assert selected
    assert reason == "accepted"


def test_recursive_cell_ocr_passes_left_and_upper_text_context():
    image = Image.new("L", (120, 100), "white")
    draw = ImageDraw.Draw(image)
    draw.text((8, 18), "a", fill="black")
    draw.text((68, 18), "b", fill="black")
    draw.text((68, 68), "c", fill="black")
    table = TableLayout(
        bbox=(0, 0, 120, 100),
        rows=2,
        cols=2,
        x_lines=(0, 60, 120),
        y_lines=(0, 50, 100),
        cells=(
            TableCell(row=0, col=0, bbox=(0, 0, 60, 50)),
            TableCell(row=0, col=1, bbox=(60, 0, 120, 50)),
            TableCell(row=1, col=0, bbox=(0, 50, 60, 100), sparse_code=EMPTY_SLOT_CODE),
            TableCell(row=1, col=1, bbox=(60, 50, 120, 100)),
        ),
    )
    contexts: list[tuple[str, ...]] = []

    class FakeEngine:
        def __init__(self):
            self.context: tuple[str, ...] = ()

        def language_context(self, *parts: str):
            engine = self

            class Scope:
                def __enter__(self):
                    self.previous = engine.context
                    engine.context = tuple(part for part in parts if part)

                def __exit__(self, *_args):
                    engine.context = self.previous

            return Scope()

        def recognize(self, _image, mode="text_mode", psm=6):
            contexts.append(self.context)
            return f"cell-{len(contexts)}"

    rows, calls = recognize_table_cells(FakeEngine(), image, table)

    assert calls == 3
    assert rows == [["cell-1", "cell-2"], ["", "cell-3"]]
    assert contexts == [(), ("cell-1",), ("cell-2",)]
