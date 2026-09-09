import math
import os
from dataclasses import dataclass

from PIL import Image, ImageEnhance, ImageFilter

from app.sparse_pipeline.contracts import AffineTransform, Matrix3

DEFAULT_MAX_DEWARP_PIXELS = 16_000_000
TEXT_PROJECTOR_EDGE_DENSITY = 0.08
REGION_DESKEW_MIN_CONFIDENCE = 0.82
REGION_DESKEW_MIN_DEGREES = 1.0


def _matrix_product(first: Matrix3, second: Matrix3) -> Matrix3:
    return tuple(
        sum(first[row * 3 + inner] * second[inner * 3 + column] for inner in range(3))
        for row in range(3)
        for column in range(3)
    )


def _normalized_matrix(values) -> Matrix3:
    flattened = tuple(float(value) for value in values)
    if len(flattened) != 9:
        raise ValueError("transform matrix must contain nine values")
    scale = flattened[8]
    if abs(scale) < 1e-12:
        raise ValueError("transform matrix has a zero projective scale")
    return tuple(value / scale for value in flattened)


@dataclass(frozen=True)
class RasterTransform(AffineTransform):
    """One raw-source to aligned-raster transform, affine or projective."""

    operation: str = "identity"
    confidence: float = 1.0
    source_angle_degrees: float = 0.0
    residual_angle_degrees: float = 0.0

    def __post_init__(self) -> None:
        if min(*self.original_size, *self.aligned_size) < 1:
            raise ValueError("transform image sizes must be positive")
        for matrix in (self.forward, self.inverse):
            if len(matrix) != 9 or not all(math.isfinite(value) for value in matrix):
                raise ValueError("transform matrices must contain nine finite values")
        identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        for first, second in (
            (self.forward, self.inverse),
            (self.inverse, self.forward),
        ):
            product = _normalized_matrix(_matrix_product(first, second))
            if any(abs(value - expected) > 1e-7 for value, expected in zip(product, identity)):
                raise ValueError("forward and inverse matrices are not inverses")
        if type(self.operation) is not str or not self.operation:
            raise ValueError("transform operation must be a non-empty string")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("transform confidence must be between zero and one")
        if not all(
            math.isfinite(value)
            for value in (
                self.source_angle_degrees,
                self.residual_angle_degrees,
            )
        ):
            raise ValueError("transform angles must be finite")

    @classmethod
    def compose(
        cls,
        region: "RasterTransform",
        alignment: AffineTransform,
        *,
        alignment_degrees: float,
    ) -> "RasterTransform":
        if region.aligned_size != alignment.original_size:
            raise ValueError("transform chain has incompatible canvas sizes")
        forward = _normalized_matrix(_matrix_product(alignment.forward, region.forward))
        inverse = _normalized_matrix(_matrix_product(region.inverse, alignment.inverse))
        operations = tuple(
            operation
            for operation in (
                None if region.operation == "identity" else region.operation,
                "affine-deskew" if abs(alignment_degrees) >= 1e-12 else None,
            )
            if operation is not None
        )
        return cls(
            original_size=region.original_size,
            aligned_size=alignment.aligned_size,
            forward=forward,
            inverse=inverse,
            operation="+".join(operations) if operations else "identity",
            confidence=region.confidence,
            source_angle_degrees=region.source_angle_degrees,
            residual_angle_degrees=(region.residual_angle_degrees - alignment_degrees),
        )


@dataclass(frozen=True)
class RegionPreprocessingResult:
    image: Image.Image
    transform: RasterTransform
    applied: bool
    gate_reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.image, Image.Image):
            raise TypeError("preprocessed image must be a Pillow Image")
        if not isinstance(self.transform, RasterTransform):
            raise TypeError("preprocessing transform must be a RasterTransform")
        if self.image.size != self.transform.aligned_size:
            raise ValueError("preprocessed image and transform sizes disagree")
        if self.applied != (self.transform.operation != "identity"):
            raise ValueError("preprocessing application state disagrees with transform")
        if type(self.gate_reason) is not str or not self.gate_reason:
            raise ValueError("preprocessing gate reason must be non-empty")


def confidence_gated_region_deskew(
    image: Image.Image,
    *,
    enabled: bool = True,
    minimum_confidence: float = REGION_DESKEW_MIN_CONFIDENCE,
    minimum_degrees: float = REGION_DESKEW_MIN_DEGREES,
    maximum_pixels: int | None = None,
) -> RegionPreprocessingResult:
    """Rectify one confidently bounded document region before geometry."""

    if not isinstance(image, Image.Image):
        raise TypeError("image must be a Pillow Image")
    if type(enabled) is not bool:
        raise TypeError("enabled must be a boolean")
    if not math.isfinite(minimum_confidence) or not 0.0 <= minimum_confidence <= 1.0:
        raise ValueError("minimum confidence must be between zero and one")
    if not math.isfinite(minimum_degrees) or not 0.0 <= minimum_degrees <= 15.0:
        raise ValueError("minimum degrees must be between zero and 15")
    pixel_limit = max_dewarp_pixels() if maximum_pixels is None else maximum_pixels
    if type(pixel_limit) is not int or pixel_limit <= 0:
        raise ValueError("maximum pixels must be a positive integer")

    identity = RasterTransform.identity(image.size)
    if not enabled:
        return RegionPreprocessingResult(image, identity, False, "disabled")
    if min(image.size) < 300:
        return RegionPreprocessingResult(image, identity, False, "dimension-gate")
    if image.width * image.height > pixel_limit:
        return RegionPreprocessingResult(image, identity, False, "pixel-gate")

    source = _detected_projector_quad(image)
    detector = "projector"
    detector_floor = 0.55
    if source is None:
        source = _detected_document_quad(image)
        detector = "document"
        detector_floor = 0.35
    if source is None:
        return RegionPreprocessingResult(image, identity, False, "no-region")

    metrics = _quad_metrics(source, image.size, detector_floor=detector_floor)
    if metrics["confidence"] < minimum_confidence:
        return RegionPreprocessingResult(
            image,
            identity,
            False,
            f"confidence-gate:{metrics['confidence']:.3f}",
        )
    if metrics["correction_magnitude"] < minimum_degrees:
        return RegionPreprocessingResult(
            image,
            identity,
            False,
            f"angle-gate:{metrics['correction_magnitude']:.3f}",
        )
    target_size = metrics["target_size"]
    if target_size[0] * target_size[1] > pixel_limit:
        return RegionPreprocessingResult(image, identity, False, "output-pixel-gate")
    if _is_suspicious_horizontal_dewarp_crop(image.size, target_size):
        return RegionPreprocessingResult(image, identity, False, "aspect-gate")

    return _warp_region_with_contract(
        image,
        source,
        target_size,
        operation=f"region-{detector}-dewarp",
        confidence=metrics["confidence"],
        source_angle_degrees=metrics["source_angle_degrees"],
    )


def _quad_metrics(
    source,
    image_size: tuple[int, int],
    *,
    detector_floor: float,
) -> dict[str, object]:
    width, height = image_size
    top_width = math.dist(source[0], source[1])
    bottom_width = math.dist(source[3], source[2])
    left_height = math.dist(source[0], source[3])
    right_height = math.dist(source[1], source[2])
    target_width = max(1, round((top_width + bottom_width) / 2))
    target_height = max(1, round((left_height + right_height) / 2))
    polygon_area = (
        abs(
            sum(
                source[index][0] * source[(index + 1) % 4][1] - source[(index + 1) % 4][0] * source[index][1]
                for index in range(4)
            )
        )
        / 2.0
    )
    area_ratio = polygon_area / max(1.0, width * height)
    edge_balance = min(
        min(top_width, bottom_width) / max(top_width, bottom_width, 1.0),
        min(left_height, right_height) / max(left_height, right_height, 1.0),
    )
    top_angle = math.degrees(
        math.atan2(
            source[1][1] - source[0][1],
            source[1][0] - source[0][0],
        )
    )
    bottom_angle = math.degrees(
        math.atan2(
            source[2][1] - source[3][1],
            source[2][0] - source[3][0],
        )
    )
    source_angle = (top_angle + bottom_angle) / 2.0
    perspective_spread = abs(top_angle - bottom_angle)
    correction_magnitude = max(
        abs(top_angle),
        abs(bottom_angle),
        perspective_spread,
    )
    area_score = min(1.0, area_ratio / 0.5)
    confidence = min(
        1.0,
        detector_floor + 0.25 * area_score + 0.20 * edge_balance,
    )
    return {
        "confidence": confidence,
        "correction_magnitude": correction_magnitude,
        "source_angle_degrees": source_angle,
        "target_size": (target_width, target_height),
    }


def _warp_region_with_contract(
    image: Image.Image,
    source,
    target_size: tuple[int, int],
    *,
    operation: str,
    confidence: float,
    source_angle_degrees: float,
) -> RegionPreprocessingResult:
    target_width, target_height = target_size
    destination = (
        (0.0, 0.0),
        (float(target_width - 1), 0.0),
        (float(target_width - 1), float(target_height - 1)),
        (0.0, float(target_height - 1)),
    )
    coefficients = _perspective_coefficients(destination, source)
    inverse = _normalized_matrix(
        (
            coefficients[0],
            coefficients[1],
            coefficients[2],
            coefficients[3],
            coefficients[4],
            coefficients[5],
            coefficients[6],
            coefficients[7],
            1.0,
        )
    )
    import numpy as np

    forward = _normalized_matrix(np.linalg.inv(np.asarray(inverse, dtype=np.float64).reshape(3, 3)).reshape(-1))
    transform = RasterTransform(
        original_size=image.size,
        aligned_size=target_size,
        forward=forward,
        inverse=inverse,
        operation=operation,
        confidence=confidence,
        source_angle_degrees=source_angle_degrees,
        residual_angle_degrees=0.0,
    )
    processed = image.transform(
        target_size,
        Image.Transform.PERSPECTIVE,
        coefficients,
        getattr(Image, "Resampling", Image).BICUBIC,
    ).convert("RGB")
    return RegionPreprocessingResult(processed, transform, True, "applied")


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

        detected_source = _detected_projector_quad(image)
        if detected_source is None:
            source = tuple((int(width * x), int(height * y)) for x, y in _projector_slide_source_ratios(gray))
            target_width, target_height = 2000, 1200
        else:
            source = detected_source
            top_width = math.dist(source[0], source[1])
            bottom_width = math.dist(source[3], source[2])
            left_height = math.dist(source[0], source[3])
            right_height = math.dist(source[1], source[2])
            target_width = max(250, round((top_width + bottom_width) / 2))
            target_height = max(
                180,
                round((left_height + right_height) / 2),
            )
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


class RecursivePageDewarpStep(ImagePreprocessingStep):
    """The region-local dewarp used by the frozen v16 recursive grid."""

    name = "recursive_page_dewarp"

    def apply(self, image: Image.Image) -> Image.Image:
        if min(image.size) < 220:
            return image
        if image.size[0] * image.size[1] > max_dewarp_pixels():
            return image

        source = _detected_projector_quad(image)
        if source is not None:
            top_width = math.dist(source[0], source[1])
            bottom_width = math.dist(source[3], source[2])
            left_height = math.dist(source[0], source[3])
            right_height = math.dist(source[1], source[2])
            target_width = max(250, round((top_width + bottom_width) / 2))
            target_height = max(
                180,
                round((left_height + right_height) / 2),
            )
            destination = (
                (0, 0),
                (target_width, 0),
                (target_width, target_height),
                (0, target_height),
            )
            coefficients = _perspective_coefficients(destination, source)
            return image.transform(
                (target_width, target_height),
                Image.Transform.PERSPECTIVE,
                coefficients,
                getattr(Image, "Resampling", Image).BICUBIC,
            ).convert("RGB")

        return ProjectedDocumentDewarpStep().apply(image)


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


def _detected_projector_quad(
    image: Image.Image,
) -> tuple[tuple[int, int], ...] | None:
    """Detect the finite blue/green projector field before dewarping."""

    import numpy as np

    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    height, width = rgb.shape[:2]
    if height < 220 or width < 220:
        return None

    red = rgb[:, :, 0]
    green = rgb[:, :, 1]
    blue = rgb[:, :, 2]
    field = (green - red > 16) & (blue - red > 28) & (blue > 105)
    window = max(15, min(61, (height // 40) | 1))
    padded = np.pad(
        field.astype(np.int16),
        ((window // 2, window // 2), (0, 0)),
        mode="constant",
    )
    cumulative = np.vstack(
        (
            np.zeros((1, width), dtype=np.int32),
            np.cumsum(padded, axis=0, dtype=np.int32),
        )
    )
    smooth = cumulative[window:] - cumulative[:-window]
    active = smooth >= max(3, round(window * 0.25))
    any_active = np.any(active, axis=0)
    first = np.argmax(active, axis=0)
    last = height - 1 - np.argmax(active[::-1], axis=0)
    valid = any_active & ((last - first) >= height * 0.35)
    columns = np.flatnonzero(valid)
    if columns.size < width * 0.35:
        return None

    left = int(columns[0])
    right = int(columns[-1])
    if right - left < width * 0.45:
        return None

    top_fit = _robust_line_fit(columns, first[columns])
    bottom_fit = _robust_line_fit(columns, last[columns])
    if top_fit is None or bottom_fit is None:
        return None
    top_slope, top_intercept = top_fit
    bottom_slope, bottom_intercept = bottom_fit
    inset = max(2, round(window * 0.28))
    top_left = int(round(top_slope * left + top_intercept + inset))
    top_right = int(round(top_slope * right + top_intercept + inset))
    bottom_left = int(round(bottom_slope * left + bottom_intercept - inset))
    bottom_right = int(round(bottom_slope * right + bottom_intercept - inset))
    top_left = max(0, min(height - 1, top_left))
    top_right = max(0, min(height - 1, top_right))
    bottom_left = max(0, min(height - 1, bottom_left))
    bottom_right = max(0, min(height - 1, bottom_right))
    if min(bottom_left - top_left, bottom_right - top_right) < height * 0.25:
        return None

    margin = max(8, round(min(width, height) * 0.025))
    near_full_frame = (
        left <= margin
        and right >= width - 1 - margin
        and max(top_left, top_right) <= margin
        and min(bottom_left, bottom_right) >= height - 1 - margin
    )
    if near_full_frame:
        return None

    return (
        (left, top_left),
        (right, top_right),
        (right, bottom_right),
        (left, bottom_left),
    )


def _detected_document_quad(
    image: Image.Image,
) -> tuple[tuple[float, float], ...] | None:
    """Detect a bounded bright page only when its contour is unambiguous."""

    try:
        import cv2
        import numpy as np
    except Exception:
        return None

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    height, width = gray.shape[:2]
    image_area = width * height
    for threshold in (180, 160, 140, 120):
        mask = cv2.inRange(blurred, threshold, 255)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        contours, _ = cv2.findContours(
            mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:3]:
            area = float(cv2.contourArea(contour))
            area_ratio = area / max(1, image_area)
            if area_ratio < 0.35 or area_ratio > 0.90:
                continue
            perimeter = cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, 0.015 * perimeter, True)
            if len(approx) != 4 or not cv2.isContourConvex(approx):
                continue
            corners = _order_quad(approx.reshape(4, 2))
            if _is_near_full_frame_quad(corners, width, height, area_ratio):
                continue
            bounding = cv2.boundingRect(approx)
            rectangularity = area / max(1, bounding[2] * bounding[3])
            if rectangularity < 0.72:
                continue
            return tuple((float(point[0]), float(point[1])) for point in corners)
    return None


def _robust_line_fit(x, y) -> tuple[float, float] | None:
    import numpy as np

    if len(x) < 8:
        return None
    selected = np.ones(len(x), dtype=bool)
    for _ in range(3):
        if int(np.count_nonzero(selected)) < 8:
            return None
        slope, intercept = np.polyfit(x[selected], y[selected], 1)
        residual = np.abs(y - (slope * x + intercept))
        scale = float(np.median(residual[selected]))
        selected = residual <= max(3.0, scale * 3.0)
    return float(slope), float(intercept)


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
    RecursivePageDewarpStep.name: RecursivePageDewarpStep,
}
