from app.engines.base import OcrEngine


class EasyOcrEngine(OcrEngine):
    """
    EasyOCR engine with support for multiple languages including Cyrillic.
    EasyOCR uses deep learning models for text detection and recognition.
    """

    def __init__(self, languages=None, download_enabled: bool = True):
        """
        Initialize EasyOCR engine.

        Args:
            languages: List of language codes (default: ['en', 'ru'] for English and Russian)
        """
        if languages is None:
            languages = ["en", "ru"]
        self.languages = languages
        self.download_enabled = download_enabled
        self._reader = None
        self._available = False
        self._init_error = None

    def _get_reader(self):
        """Lazy initialization of EasyOCR reader."""
        if self._reader is None:
            try:
                import easyocr
                import torch

                # Support CUDA (NVIDIA) and MPS (Apple Silicon).
                # Some PyTorch ROCm builds report ROCm devices through CUDA APIs.
                # easyocr just takes gpu=True/False
                use_gpu = torch.cuda.is_available() or (
                    hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
                )
                self._reader = easyocr.Reader(
                    self.languages,
                    gpu=use_gpu,
                    download_enabled=self.download_enabled,
                )
                self._available = True
            except Exception as e:
                self._init_error = str(e)
                self._available = False
        return self._reader

    @staticmethod
    def _is_dense_ocr_slot(image) -> bool:
        import numpy as np

        if image.ndim < 2 or image.shape[0] == 0 or image.shape[1] == 0:
            return False

        if image.ndim == 2:
            grayscale = image.astype(np.float32)
        else:
            grayscale = image[..., :3].astype(np.float32).mean(axis=2)
        border = np.concatenate((grayscale[0], grayscale[-1], grayscale[:, 0], grayscale[:, -1]))
        background = float(np.median(border))
        foreground = np.abs(grayscale - background) >= 24.0
        ys, xs = np.nonzero(foreground)
        if len(xs) == 0:
            return False

        height, width = grayscale.shape
        foreground_width = int(xs.max() - xs.min() + 1)
        foreground_height = int(ys.max() - ys.min() + 1)
        row_density = float(np.median(foreground[ys.min() : ys.max() + 1].mean(axis=1)))
        foreground_density = float(foreground.mean())
        return (
            foreground_width >= width * 0.70
            and foreground_height >= height * 0.70
            and row_density >= 0.15
            and 0.08 <= foreground_density <= 0.80
        )

    @staticmethod
    def _recognize_dense_slot(reader, image):
        import cv2
        import numpy as np

        height, width = image.shape[:2]
        pad_y = max(8, round(height * 0.05))
        pad_x = max(8, round(width * 0.05))

        if image.ndim == 2:
            border = np.concatenate((image[0], image[-1], image[:, 0], image[:, -1]))
            padded = np.empty((height + 2 * pad_y, width + 2 * pad_x), dtype=image.dtype)
        else:
            border = np.concatenate((image[0], image[-1], image[:, 0], image[:, -1]), axis=0)
            padded = np.empty(
                (height + 2 * pad_y, width + 2 * pad_x, image.shape[2]),
                dtype=image.dtype,
            )
        padded[...] = np.median(border, axis=0)
        padded[pad_y : pad_y + height, pad_x : pad_x + width] = image

        best = None
        for horizontal_scale in (1.0, 2.0):
            candidate = (
                padded
                if horizontal_scale == 1.0
                else cv2.resize(
                    padded,
                    None,
                    fx=horizontal_scale,
                    fy=1.0,
                    interpolation=cv2.INTER_LINEAR,
                )
            )
            candidate_height, candidate_width = candidate.shape[:2]
            rows = reader.recognize(
                candidate,
                horizontal_list=[[0, candidate_width, 0, candidate_height]],
                free_list=[],
                decoder="greedy",
                reformat=True,
            )
            for _, text, confidence in rows:
                normalized = text.strip()
                if normalized and (best is None or float(confidence) > best[1]):
                    best = (normalized, float(confidence))

        if best is None:
            return []
        box = [[0, 0], [width, 0], [width, height], [0, height]]
        return [(box, best[0], best[1])]

    def _readtext(self, reader, image):
        direct = self._recognize_dense_slot(reader, image) if self._is_dense_ocr_slot(image) else []
        if direct and direct[0][2] >= 0.97:
            return direct

        detected = reader.readtext(image)
        if direct and len(detected) <= 1:
            detected_confidence = max((float(row[2]) for row in detected), default=0.0)
            if direct[0][2] >= detected_confidence + 0.10:
                return direct
        return detected

    def recognize(self, image, mode: str = "text_mode", psm: int = 6) -> str:
        """
        Recognize text in image using EasyOCR.

        Args:
            image: PIL Image object
            mode: Recognition mode ('text_mode', 'receipt_mode', etc.)
            psm: Page segmentation mode (kept for compatibility, not used by EasyOCR)

        Returns:
            Recognized text as string
        """
        reader = self._get_reader()
        if reader is None:
            return ""

        try:
            # Convert PIL Image to numpy array if needed
            import numpy as np

            img_array = np.array(image)

            # EasyOCR readtext returns list of (bbox, text, confidence)
            result = self._readtext(reader, img_array)

            # Filter by confidence and build text
            min_conf = 0.3 if mode == "receipt_mode" else 0.4

            lines = []
            current_line = []
            last_y = None
            y_threshold = 20  # pixels to consider same line

            # Sort by y-coordinate first, then x
            result.sort(key=lambda x: (x[0][0][1], x[0][0][0]))

            for bbox, text, conf in result:
                if conf < min_conf:
                    continue

                # Get bounding box center y
                y_center = (bbox[0][1] + bbox[2][1]) / 2

                if last_y is None or abs(y_center - last_y) > y_threshold:
                    if current_line:
                        lines.append(" ".join(current_line))
                        current_line = []

                current_line.append(text)
                last_y = y_center

            if current_line:
                lines.append(" ".join(current_line))

            return "\n".join(lines)

        except Exception as e:
            print(f"EasyOCR recognition error: {e}")
            return ""

    def recognize_words(self, image, psm: int = 6, min_conf: int = 20) -> list[dict]:
        reader = self._get_reader()
        if reader is None:
            return []

        try:
            import numpy as np

            min_conf_ratio = min_conf / 100
            image_width, image_height = image.size
            result = self._readtext(reader, np.array(image))
            words = []
            for bbox, text, conf in result:
                if conf < min_conf_ratio or not text.strip():
                    continue

                xs = [point[0] for point in bbox]
                ys = [point[1] for point in bbox]
                left = max(0, min(image_width, int(min(xs))))
                top = max(0, min(image_height, int(min(ys))))
                right = max(0, min(image_width, int(max(xs))))
                bottom = max(0, min(image_height, int(max(ys))))
                if right <= left or bottom <= top:
                    continue
                words.append(
                    {
                        "text": text.strip(),
                        "bbox": (left, top, right, bottom),
                        "conf": float(conf * 100),
                    }
                )

            return words
        except Exception as e:
            print(f"EasyOCR word recognition error: {e}")
            return []

    def available(self) -> bool:
        """Check if EasyOCR is available."""
        if self._reader is None:
            self._get_reader()
        return self._available

    def info(self) -> dict:
        """Return engine information."""
        gpu_status = "unknown"
        if self._reader is not None:
            try:
                import torch

                gpu_status = (
                    "active"
                    if torch.cuda.is_available()
                    or (hasattr(torch.backends, "mps") and torch.backends.mps.is_available())
                    else "inactive"
                )
            except Exception:
                gpu_status = "error_detecting"

        return {
            "engine": "easyocr",
            "languages": self.languages,
            "available": self.available(),
            "gpu": gpu_status,
            "init_error": self._init_error,
        }
