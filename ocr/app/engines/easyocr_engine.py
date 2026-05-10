import re

from PIL import Image

from app.engines.base import OcrEngine


class EasyOcrEngine(OcrEngine):
    """
    EasyOCR engine with support for multiple languages including Cyrillic.
    EasyOCR uses deep learning models for text detection and recognition.
    """

    def __init__(self, languages=None):
        """
        Initialize EasyOCR engine.

        Args:
            languages: List of language codes (default: ['en', 'ru'] for English and Russian)
        """
        if languages is None:
            languages = ["en", "ru"]
        self.languages = languages
        self._reader = None
        self._available = False
        self._init_error = None

    def _get_reader(self):
        """Lazy initialization of EasyOCR reader."""
        if self._reader is None:
            try:
                import easyocr
                import torch

                # Support CUDA (NVIDIA) and MPS (Apple Silicon), ROCm is handled as CUDA in some PyTorch builds or has separate checks
                # easyocr just takes gpu=True/False
                use_gpu = torch.cuda.is_available() or (
                    hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
                )
                self._reader = easyocr.Reader(self.languages, gpu=use_gpu)
                self._available = True
            except Exception as e:
                self._init_error = str(e)
                self._available = False
        return self._reader

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
            result = reader.readtext(img_array)

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
