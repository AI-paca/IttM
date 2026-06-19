import math
import os

from PIL import Image, ImageEnhance, ImageFilter

DEFAULT_MAX_DEWARP_PIXELS = 16_000_000
TEXT_PROJECTOR_EDGE_DENSITY = 0.08


def max_dewarp_pixels() -> int:
    raw_value = os.environ.get(
        "OCR_MAX_DEWARP_PIXELS",
        str(DEFAULT_MAX_DEWARP_PIXELS),
    )
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("OCR_MAX_DEWARP_PIXELS must be an integer") from exc
    if value <= 0:
        raise RuntimeError("OCR_MAX_DEWARP_PIXELS must be greater than zero")
    return value


class ImagePreprocessingStep:
    name = "base"

    def apply(self, image: Image.Image) -> Image.Image:
        return image


class ProjectedDocumentDewarpStep(ImagePreprocessingStep):
    name = "projected_document_dewarp"

    def apply(self, image: Image.Image) -> Image.Image:
        if _is_dewarped_projector_slide_size(image.size):
            return image
        if min(image.size) < 300:
            return image
        if image.size[0] * image.size[1] > max_dewarp_pixels():
            return image

        try:
            import cv2
            import numpy as np
        except Exception:
            return image

        rgb = np.array(image.convert("RGB"))
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        height, width = gray.shape[:2]
        image_area = width * height

        best_corners = None
        for threshold in (180, 160, 140, 120):
            mask = cv2.inRange(blurred, threshold, 255)
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
                area = float(cv2.contourArea(contour))
                area_ratio = area / max(1, image_area)
                if area_ratio < 0.18 or area_ratio > 0.92:
                    continue

                perimeter = cv2.arcLength(contour, True)
                approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
                if len(approx) != 4:
                    continue

                corners = _order_quad(approx.reshape(4, 2))
                if _is_near_full_frame_quad(corners, width, height, area_ratio):
                    continue
                best_corners = corners
                break

            if best_corners is not None:
                break

        if best_corners is None:
            return image

        top_width = float(np.linalg.norm(best_corners[1] - best_corners[0]))
        bottom_width = float(np.linalg.norm(best_corners[2] - best_corners[3]))
        left_height = float(np.linalg.norm(best_corners[3] - best_corners[0]))
        right_height = float(np.linalg.norm(best_corners[2] - best_corners[1]))
        target_width = int(round((top_width + bottom_width) / 2))
        target_height = int(round((left_height + right_height) / 2))

        if target_width < 250 or target_height < 180:
            return image
        if _is_suspicious_horizontal_dewarp_crop(
            (width, height),
            (target_width, target_height),
        ):
            return image

        destination = np.array(
            [
                [0, 0],
                [target_width - 1, 0],
                [target_width - 1, target_height - 1],
                [0, target_height - 1],
            ],
            dtype="float32",
        )
        matrix = cv2.getPerspectiveTransform(best_corners, destination)
        warped = cv2.warpPerspective(
            bgr,
            matrix,
            (target_width, target_height),
            borderValue=(255, 255, 255),
        )
        warped_gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
        warped_gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(warped_gray)
        return Image.fromarray(warped_gray).convert("RGB")


class SmallTextUpscaleStep(ImagePreprocessingStep):
    name = "small_text_upscale"

    def apply(self, image: Image.Image) -> Image.Image:
        width, height = image.size
        if min(width, height) >= 240:
            return image
        if max(width, height) > 900:
            return image
        if width * height > 300_000:
            return image

        scale = max(2, min(4, math.ceil(600 / max(1, min(width, height)))))
        resample = getattr(Image, "Resampling", Image).LANCZOS
        resized = image.resize((width * scale, height * scale), resample)
        return ImageEnhance.Contrast(resized).enhance(1.8).convert("RGB")


class MobileScreenUpscaleStep(ImagePreprocessingStep):
    name = "mobile_screen_upscale"

    def apply(self, image: Image.Image) -> Image.Image:
        width, height = image.size
        aspect = height / max(1, width)
        if not (650 <= width <= 900 and 900 <= height <= 1300 and 1.2 <= aspect <= 1.7):
            return image

        resample = getattr(Image, "Resampling", Image).LANCZOS
        return image.resize((width * 2, height * 2), resample).convert("RGB")


class ProjectorSlideDewarpStep(ImagePreprocessingStep):
    name = "projector_slide_dewarp"

    def apply(self, image: Image.Image) -> Image.Image:
        width, height = image.size
        aspect = height / max(1, width)
        if not (850 <= width <= 1200 and 1100 <= height <= 1500 and 1.2 <= aspect <= 1.6):
            return image

        gray = image.convert("L")
        center = gray.crop((width // 5, height // 5, width * 4 // 5, height * 4 // 5))
        if center.resize((1, 1)).getpixel((0, 0)) < 120:
            return image

        source = tuple((int(width * x), int(height * y)) for x, y in _projector_slide_source_ratios(gray))
        target_width, target_height = 2000, 1200
        destination = (
            (0, 0),
            (target_width, 0),
            (target_width, target_height),
            (0, target_height),
        )
        coefficients = _perspective_coefficients(destination, source)
        resample = getattr(Image, "Resampling", Image).BICUBIC
        return image.transform(
            (target_width, target_height),
            Image.Transform.PERSPECTIVE,
            coefficients,
            resample,
        ).convert("RGB")


class OcrPreprocessingPipeline:
    def __init__(self, steps: list[ImagePreprocessingStep]):
        self.steps = steps

    @classmethod
    def from_step_names(cls, step_names: tuple[str, ...]):
        steps: list[ImagePreprocessingStep] = []
        for name in step_names:
            step = IMAGE_PREPROCESSING_STEPS.get(name)
            if step is None:
                known_steps = ", ".join(sorted(IMAGE_PREPROCESSING_STEPS))
                raise ValueError(f"Unknown image preprocessing step '{name}'. Known steps: {known_steps}")
            steps.append(step())
        return cls(steps)

    def apply(self, image: Image.Image) -> Image.Image:
        processed = image
        for step in self.steps:
            processed = step.apply(processed)
        return processed


def _order_quad(points):
    import numpy as np

    pts = np.array(points, dtype="float32").reshape(4, 2)
    ordered = np.zeros((4, 2), dtype="float32")
    point_sums = pts.sum(axis=1)
    point_diffs = np.diff(pts, axis=1).reshape(4)

    ordered[0] = pts[int(np.argmin(point_sums))]
    ordered[2] = pts[int(np.argmax(point_sums))]
    ordered[1] = pts[int(np.argmin(point_diffs))]
    ordered[3] = pts[int(np.argmax(point_diffs))]
    return ordered


def _perspective_coefficients(destination, source):
    import numpy as np

    matrix = []
    for (x, y), (u, v) in zip(destination, source):
        matrix.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        matrix.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    return tuple(np.linalg.solve(np.array(matrix, dtype=float), np.array(source).reshape(8)))


def _is_dewarped_projector_slide_size(size: tuple[int, int]) -> bool:
    width, height = size
    aspect = height / max(1, width)
    return 1800 <= width <= 2200 and 1000 <= height <= 1400 and 0.5 <= aspect <= 0.75


def _is_suspicious_horizontal_dewarp_crop(
    source_size: tuple[int, int],
    target_size: tuple[int, int],
) -> bool:
    source_width, source_height = source_size
    target_width, target_height = target_size
    source_aspect = source_width / max(1, source_height)
    target_aspect = target_width / max(1, target_height)
    return source_aspect <= 2.0 and target_aspect >= 2.8


def _edge_density(image: Image.Image) -> float:
    edges = image.filter(ImageFilter.FIND_EDGES)
    histogram = edges.histogram()
    total = sum(histogram)
    if total <= 0:
        return 0.0
    return sum(histogram[31:]) / total


def _projector_slide_source_ratios(
    gray: Image.Image,
) -> tuple[tuple[float, float], ...]:
    width, height = gray.size
    content = gray.crop((int(width * 0.05), int(height * 0.15), int(width * 0.95), int(height * 0.88)))
    density = _edge_density(content)
    if density >= TEXT_PROJECTOR_EDGE_DENSITY:
        return (
            (0.03, 0.28),
            (1.0, 0.15),
            (1.0, 0.88),
            (0.03, 0.78),
        )
    return (
        (0.0, 0.16),
        (1.0, 0.027),
        (1.0, 0.855),
        (0.0, 0.793),
    )


def _is_near_full_frame_quad(corners, width: int, height: int, area_ratio: float) -> bool:
    if area_ratio < 0.85:
        return False

    tolerance = max(12, int(min(width, height) * 0.02))
    expected = ((0, 0), (width - 1, 0), (width - 1, height - 1), (0, height - 1))
    return all(
        abs(float(x) - ex) <= tolerance and abs(float(y) - ey) <= tolerance
        for (x, y), (ex, ey) in zip(corners, expected)
    )


IMAGE_PREPROCESSING_STEPS: dict[str, type[ImagePreprocessingStep]] = {
    ProjectorSlideDewarpStep.name: ProjectorSlideDewarpStep,
    MobileScreenUpscaleStep.name: MobileScreenUpscaleStep,
    SmallTextUpscaleStep.name: SmallTextUpscaleStep,
    ProjectedDocumentDewarpStep.name: ProjectedDocumentDewarpStep,
}
