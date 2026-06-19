from PIL import Image, ImageDraw

from app.preprocessing import (
    MobileScreenUpscaleStep,
    ProjectedDocumentDewarpStep,
    ProjectorSlideDewarpStep,
    _is_suspicious_horizontal_dewarp_crop,
    SmallTextUpscaleStep,
    _projector_slide_source_ratios,
)


def test_dewarp_skips_images_above_its_working_pixel_budget(monkeypatch):
    class LargeImage:
        size = (5000, 4000)

    image = LargeImage()
    monkeypatch.setenv("OCR_MAX_DEWARP_PIXELS", "16000000")

    assert ProjectedDocumentDewarpStep().apply(image) is image


def test_small_text_upscale_enlarges_only_tiny_debug_tables():
    tiny = Image.new("RGB", (149, 316), "white")
    wide = Image.new("RGB", (1072, 77), "white")

    try:
        enlarged = SmallTextUpscaleStep().apply(tiny)
        untouched = SmallTextUpscaleStep().apply(wide)
        assert enlarged.size == (596, 1264)
        assert untouched.size == (1072, 77)
    finally:
        tiny.close()
        wide.close()
        enlarged.close()
        if untouched is not wide:
            untouched.close()


def test_mobile_screen_upscale_targets_medium_coupon_screens_only():
    coupon = Image.new("RGB", (755, 1072), "white")
    small_coupon = Image.new("RGB", (474, 619), "white")
    photo_like = Image.new("RGB", (960, 1280), "white")

    try:
        enlarged = MobileScreenUpscaleStep().apply(coupon)
        untouched_small = MobileScreenUpscaleStep().apply(small_coupon)
        untouched_photo = MobileScreenUpscaleStep().apply(photo_like)
        assert enlarged.size == (1510, 2144)
        assert untouched_small is small_coupon
        assert untouched_photo is photo_like
    finally:
        coupon.close()
        small_coupon.close()
        photo_like.close()
        enlarged.close()


def test_projector_slide_dewarp_targets_projector_photo_shape_only():
    projector_photo = Image.new("RGB", (960, 1280), (210, 220, 220))
    dark_photo = Image.new("RGB", (960, 1280), (40, 40, 40))
    wide_image = Image.new("RGB", (1600, 900), "white")

    try:
        dewarped = ProjectorSlideDewarpStep().apply(projector_photo)
        untouched_dark = ProjectorSlideDewarpStep().apply(dark_photo)
        untouched_wide = ProjectorSlideDewarpStep().apply(wide_image)

        assert dewarped.size == (2000, 1200)
        assert untouched_dark is dark_photo
        assert untouched_wide is wide_image
    finally:
        projector_photo.close()
        dark_photo.close()
        wide_image.close()
        dewarped.close()


def test_projected_document_dewarp_skips_dewarped_projector_slide_canvas():
    image = Image.new("RGB", (2000, 1200), "white")

    try:
        assert ProjectedDocumentDewarpStep().apply(image) is image
    finally:
        image.close()


def test_projected_document_dewarp_rejects_wide_band_crop_from_normal_page():
    assert _is_suspicious_horizontal_dewarp_crop((1530, 984), (1529, 415))
    assert not _is_suspicious_horizontal_dewarp_crop((2000, 1200), (2000, 1200))
    assert not _is_suspicious_horizontal_dewarp_crop((2400, 900), (2200, 650))


def test_projector_slide_dewarp_uses_text_slide_geometry_for_dense_projector_photo():
    text_slide = Image.new("L", (960, 1280), 190)
    draw = ImageDraw.Draw(text_slide)
    for y in range(230, 980, 12):
        draw.line((80, y, 860, y), fill=70, width=3)
    for x in range(100, 860, 36):
        draw.line((x, 240, x, 980), fill=100, width=2)

    diagram_slide = Image.new("L", (960, 1280), 190)
    draw = ImageDraw.Draw(diagram_slide)
    draw.rectangle((240, 400, 720, 760), outline=120, width=4)
    draw.line((120, 960, 840, 960), fill=120, width=4)

    try:
        assert _projector_slide_source_ratios(text_slide)[0] == (0.03, 0.28)
        assert _projector_slide_source_ratios(diagram_slide)[0] == (0.0, 0.16)
    finally:
        text_slide.close()
        diagram_slide.close()
