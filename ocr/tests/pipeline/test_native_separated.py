from PIL import Image, ImageDraw

from app.pipeline_core.separated import (
    SEPARATED_STAGES,
    NativeSeparatedSession,
    SeparatedOcrWord,
    SeparatedRecognition,
    run_native_separated_pipeline,
)


def _two_line_image() -> Image.Image:
    image = Image.new("RGB", (160, 80), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((15, 12, 105, 19), fill="black")
    draw.rectangle((25, 50, 140, 58), fill="black")
    return image


def test_native_session_exposes_the_complete_separated_contract():
    image = _two_line_image()
    try:
        with NativeSeparatedSession(image) as session:
            assert len(session.jobs) == 1
            assert session.completed_stages == SEPARATED_STAGES[:5]
            index = 0
            while index < len(session.jobs):
                text = ("first", "second")[index]
                with session.raster(session.job(index)) as crop:
                    session.add_ocr_word(
                        index,
                        SeparatedOcrWord(
                            text=text,
                            bbox=(0, 0, crop.width, crop.height),
                            confidence_milli=1000,
                        ),
                    )
                session.set_ocr(index, text, 1000)
                index += 1
            assert len(session.jobs) == 2
            assert session.render() == "first\n\nsecond"
            assert session.completed_stages == SEPARATED_STAGES
    finally:
        image.close()


def test_native_runner_keeps_ocr_as_a_host_adapter():
    image = _two_line_image()
    seen = []

    def recognize(crop, job):
        seen.append((job.index, crop.size))
        text = f"segment-{job.index}"
        return SeparatedRecognition(
            text=text,
            confidence_milli=1000,
            words=(
                SeparatedOcrWord(
                    text=text,
                    bbox=(0, 0, crop.width, crop.height),
                    confidence_milli=1000,
                ),
            ),
        )

    try:
        markdown, jobs, stages = run_native_separated_pipeline(image, recognize)
    finally:
        image.close()

    assert len(jobs) == 2
    assert [index for index, _size in seen] == [0, 1]
    assert markdown == "segment-0\n\nsegment-1"
    assert stages == SEPARATED_STAGES
