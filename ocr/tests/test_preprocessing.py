from app.preprocessing import ProjectedDocumentDewarpStep


def test_dewarp_skips_images_above_its_working_pixel_budget(monkeypatch):
    class LargeImage:
        size = (5000, 4000)

    image = LargeImage()
    monkeypatch.setenv("OCR_MAX_DEWARP_PIXELS", "16000000")

    assert ProjectedDocumentDewarpStep().apply(image) is image
