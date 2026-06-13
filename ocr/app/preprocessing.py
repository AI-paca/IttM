import os

from PIL import Image

DEFAULT_MAX_DEWARP_PIXELS = 16_000_000


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
    ProjectedDocumentDewarpStep.name: ProjectedDocumentDewarpStep,
}
