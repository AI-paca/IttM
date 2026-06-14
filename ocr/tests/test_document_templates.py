from tests.document_templates import (
    generate_document_templates,
    generate_long_cart,
)


def test_generated_document_template_manifest_covers_ten_structures():
    templates = generate_document_templates()
    try:
        assert [template.name for template in templates] == [
            "receipt",
            "invoice",
            "chat",
            "ledger",
            "cv",
            "shipping-label",
            "code",
            "article",
            "slide",
            "form",
        ]
        assert all(template.expected_phrases for template in templates)
        assert all(template.image.width * template.image.height <= 1_500_000 for template in templates)
    finally:
        for template in templates:
            template.image.close()


def test_generated_long_cart_matches_realistic_scroll_geometry():
    cart = generate_long_cart()
    try:
        assert cart.image.size == (1240, 6900)
        assert cart.expected_pairs == (
            ("PRODUCT-000", "1000.99"),
            ("PRODUCT-015", "1015.99"),
            ("PRODUCT-029", "1029.99"),
        )
    finally:
        cart.image.close()
