from dataclasses import dataclass

from PIL import Image, ImageDraw

from tests.quality_fixtures import _font


@dataclass(frozen=True)
class DocumentTemplate:
    name: str
    image: Image.Image
    expected_phrases: tuple[str, ...]
    expected_pairs: tuple[tuple[str, str], ...] = ()


def _canvas(width=1400, height=800):
    return Image.new("RGB", (width, height), "white")


def _draw_lines(
    image,
    lines,
    *,
    x=70,
    y=60,
    spacing=80,
    size=44,
    fill="black",
):
    draw = ImageDraw.Draw(image)
    font = _font(size)
    for index, line in enumerate(lines):
        draw.text((x, y + index * spacing), line, fill=fill, font=font)


def generate_document_templates() -> list[DocumentTemplate]:
    templates = []

    receipt = _canvas(700, 900)
    _draw_lines(
        receipt,
        [
            "LOCAL STORE",
            "MILK ........ 120.50",
            "BREAD ....... 125.25",
            "TOTAL ....... 245.75",
            "2026-06-13",
        ],
        size=42,
    )
    templates.append(
        DocumentTemplate(
            "receipt",
            receipt,
            ("LOCAL STORE", "MILK", "BREAD", "TOTAL", "2026-06-13"),
            (("MILK", "120.50"), ("BREAD", "125.25"), ("TOTAL", "245.75")),
        )
    )

    invoice = _canvas()
    draw = ImageDraw.Draw(invoice)
    font = _font(38)
    for x in (50, 650, 900, 1300):
        draw.line((x, 80, x, 620), fill="black", width=3)
    for y in (80, 180, 300, 420, 620):
        draw.line((50, y, 1300, y), fill="black", width=3)
    rows = [
        ("ITEM", "QTY", "TOTAL"),
        ("WIDGET", "2", "199.90"),
        ("SERVICE", "1", "75.00"),
    ]
    for row, values in enumerate(rows):
        y = 105 + row * 120
        for x, value in zip((90, 700, 960), values):
            draw.text((x, y), value, fill="black", font=font)
    templates.append(
        DocumentTemplate(
            "invoice",
            invoice,
            ("ITEM", "WIDGET", "SERVICE"),
            (("WIDGET", "199.90"), ("SERVICE", "75.00")),
        )
    )

    chat = _canvas()
    _draw_lines(
        chat,
        [
            "USER: Explain bounded queues",
            "ASSISTANT: They reject excess work",
            "USER: Keep partial pages",
            "ASSISTANT: Events preserve progress",
        ],
    )
    templates.append(
        DocumentTemplate(
            "chat",
            chat,
            ("USER", "bounded queues", "ASSISTANT", "partial pages", "progress"),
        )
    )

    ledger = _canvas()
    _draw_lines(
        ledger,
        [
            "DATE DESCRIPTION AMOUNT",
            "2026-06-01 RENT -500.00",
            "2026-06-05 SALARY 1500.00",
            "2026-06-13 FOOD -120.25",
        ],
    )
    templates.append(
        DocumentTemplate(
            "ledger",
            ledger,
            ("DATE", "RENT", "SALARY", "FOOD"),
            (("RENT", "500.00"), ("SALARY", "1500.00"), ("FOOD", "120.25")),
        )
    )

    cv = _canvas()
    _draw_lines(
        cv,
        [
            "ALICE EXAMPLE",
            "SKILLS",
            "Python TypeScript Docker",
            "EXPERIENCE",
            "Senior Engineer 2022-2026",
        ],
    )
    templates.append(
        DocumentTemplate(
            "cv",
            cv,
            ("ALICE EXAMPLE", "SKILLS", "Python", "EXPERIENCE", "Engineer"),
        )
    )

    shipping = _canvas()
    _draw_lines(
        shipping,
        [
            "SHIP TO ALICE EXAMPLE",
            "MOSCOW 101000",
            "TRACK ZX123456789",
            "BOX 2 OF 3",
        ],
        size=54,
    )
    templates.append(
        DocumentTemplate(
            "shipping-label",
            shipping,
            ("SHIP TO", "ALICE EXAMPLE", "TRACK", "ZX123456789", "BOX"),
        )
    )

    code = Image.new("RGB", (1400, 800), (25, 28, 35))
    _draw_lines(
        code,
        [
            "function addNumbers(a, b) {",
            "  const total = a + b;",
            "  return total;",
            "}",
        ],
        x=70,
        y=90,
        spacing=110,
        size=48,
        fill="white",
    )
    templates.append(
        DocumentTemplate(
            "code",
            code,
            ("function", "addNumbers", "const total", "return total"),
        )
    )

    article = _canvas()
    _draw_lines(
        article,
        [
            "ROBUST DOCUMENT EXTRACTION",
            "ABSTRACT",
            "This study measures OCR quality.",
            "METHOD",
            "Generated fixtures preserve order.",
            "RESULT",
            "Digits and pairs remain readable.",
        ],
        size=38,
    )
    templates.append(
        DocumentTemplate(
            "article",
            article,
            ("ABSTRACT", "METHOD", "RESULT", "Digits", "pairs"),
        )
    )

    slide = _canvas()
    _draw_lines(
        slide,
        [
            "QUARTERLY RESULTS",
            "REVENUE 42",
            "RELIABILITY 99.9",
            "NEXT: TASK ISOLATION",
        ],
        x=100,
        y=100,
        spacing=140,
        size=64,
    )
    templates.append(
        DocumentTemplate(
            "slide",
            slide,
            ("QUARTERLY RESULTS", "REVENUE", "42", "RELIABILITY", "TASK ISOLATION"),
        )
    )

    form = _canvas()
    _draw_lines(
        form,
        [
            "APPLICATION FORM",
            "NAME: ALICE EXAMPLE",
            "[X] YES    [ ] NO",
            "REFERENCE: FORM-2026-42",
        ],
        size=52,
    )
    templates.append(
        DocumentTemplate(
            "form",
            form,
            ("APPLICATION FORM", "ALICE EXAMPLE", "YES", "NO", "FORM-2026-42"),
        )
    )

    return templates


def generate_long_cart(card_count=30) -> DocumentTemplate:
    width = 1240
    card_height = 230
    image = Image.new("RGB", (width, card_count * card_height), "white")
    draw = ImageDraw.Draw(image)
    font = _font(38)
    price_font = _font(42)
    pairs = []

    for index in range(card_count):
        top = index * card_height
        name = f"PRODUCT-{index:03d}"
        price = f"{1000 + index}.99"
        pairs.append((name, price))
        draw.rectangle((35, top + 20, width - 35, top + 195), outline="black", width=2)
        draw.text((70, top + 55), name, fill="black", font=font)
        draw.text((760, top + 55), price, fill="black", font=price_font)

    checkpoints = (pairs[0], pairs[len(pairs) // 2], pairs[-1])
    return DocumentTemplate(
        "long-cart",
        image,
        tuple(value for pair in checkpoints for value in pair),
        checkpoints,
    )
