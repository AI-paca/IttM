import re

import numpy as np
from PIL import Image

from app.engines.base import OcrEngine


class TesseractEngine(OcrEngine):
    """
    Tesseract OCR engine with card-aware processing.
    Focuses on proper preprocessing, TSV output with confidence filtering,
    and different PSM modes for different content types.
    """

    # Whitelist for receipt mode: Cyrillic, Latin, digits, common symbols
    RECEIPT_WHITELIST = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
        "0123456789.,:-%₽$()[]/+ "
    )
    BASE_LANGUAGES = ("eng", "rus")
    OPTIONAL_LANGUAGES = ("chi_sim",)

    @staticmethod
    def installed_languages() -> list:
        try:
            import pytesseract

            return pytesseract.get_languages(config="")
        except Exception:
            return []

    @classmethod
    def ocr_language_string(cls) -> str:
        installed = set(cls.installed_languages())
        languages = [lang for lang in cls.OPTIONAL_LANGUAGES if lang in installed]
        languages.extend(lang for lang in cls.BASE_LANGUAGES if lang in installed)
        return "+".join(languages or ["eng"])

    @staticmethod
    def crop_garbage_zones(image: Image.Image, left_percent: float = 0.15, right_percent: float = 0.20) -> Image.Image:
        """
        Crops garbage zones from product card image.
        Removes left part (product image) and right part (buttons like - 1 +).

        Args:
            image: Product card image
            left_percent: Percentage of width to crop from left (default 15%)
            right_percent: Percentage of width to crop from right (default 20%)

        Returns:
            Cropped image with only the text area
        """
        width, height = image.size

        left_crop = int(width * left_percent)
        right_crop = int(width * (1 - right_percent))

        # Ensure we don't crop everything
        if right_crop <= left_crop:
            return image

        return image.crop((left_crop, 0, right_crop, height))

    @staticmethod
    def filter_tsv_by_confidence(data: dict, min_conf: int = 40) -> list:
        """
        Filters TSV output by confidence level.
        Returns list of (text, conf, x, y, w, h) tuples for valid tokens.
        """
        valid_tokens = []

        for i in range(len(data["text"])):
            conf = data["conf"][i]
            text = data["text"][i].strip()

            # Skip empty text
            if not text:
                continue

            # Skip structural elements (conf == -1)
            if conf == -1:
                continue

            # Filter by confidence
            if conf < min_conf:
                # For price-like patterns, be more lenient
                if re.match(r"^\d+[.,]?\d*$", text):
                    pass  # Keep price even with low confidence
                else:
                    continue

            # Filter obvious garbage tokens
            if re.match(r"^[.?\[\]]+$", text):
                continue

            valid_tokens.append(
                (
                    text,
                    conf,
                    data["left"][i],
                    data["top"][i],
                    data["width"][i],
                    data["height"][i],
                )
            )

        return valid_tokens

    @staticmethod
    def build_text_from_tsv(data: dict, min_conf: int = 40) -> str:
        """
        Builds clean text from TSV output using confidence filtering.
        Groups tokens by line number and handles word spacing properly.
        """
        valid_tokens = TesseractEngine.filter_tsv_by_confidence(data, min_conf)

        if not valid_tokens:
            return ""

        # Group by line (using y-coordinate with tolerance)
        lines = []
        current_line = []
        current_y = None
        y_tolerance = 10

        # Sort by y then x coordinate
        valid_tokens.sort(key=lambda t: (t[3], t[2]))

        for text, _, _, y, _, _ in valid_tokens:
            if current_y is None:
                current_y = y

            if abs(y - current_y) <= y_tolerance:
                current_line.append(text)
            else:
                if current_line:
                    lines.append(" ".join(current_line))
                current_line = [text]
                current_y = y

        if current_line:
            lines.append(" ".join(current_line))

        return "\n".join(lines)

    def _preprocess_for_receipt(self, image):
        """Receipt-specific preprocessing with proper dtype handling."""
        import cv2

        # Convert PIL image to numpy array with explicit dtype
        img = np.array(image, dtype=np.uint8)

        # Convert to grayscale
        if len(img.shape) == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        else:
            gray = img

        # Enhance contrast with histogram equalization
        gray = cv2.equalizeHist(gray)

        # Adaptive thresholding (best for receipts)
        thresh = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            11,
            2,
        )

        # Remove small noise with morphological operations
        kernel = np.ones((1, 1), np.uint8)
        cleaned = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)

        return Image.fromarray(cleaned)

    def _preprocess_for_card(self, image):
        """
        Preprocessing optimized for product cards.
        Lighter preprocessing to preserve text clarity.
        """
        import cv2

        img = np.array(image, dtype=np.uint8)

        if len(img.shape) == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        else:
            gray = img

        # Simple contrast enhancement
        gray = cv2.equalizeHist(gray)

        return Image.fromarray(gray)

    def recognize_with_psm(self, image, psm: int = 6, mode: str = "text_mode") -> dict:
        """
        Runs OCR with specific PSM mode and returns TSV data.
        Optimized for speed - no preprocessing, direct OCR.
        """
        try:
            import pytesseract

            # No preprocessing for maximum speed
            # Tesseract handles image processing internally

            # Minimal config for speed
            # OEM 1 = LSTM only (faster than legacy)
            # PSM 6 = Single uniform block (good default)
            config = f"--oem 1 --psm {psm}"

            lang = self.ocr_language_string()
            data = pytesseract.image_to_data(
                image,  # Pass original image directly
                lang=lang,
                config=config,
                output_type=pytesseract.Output.DICT,
            )
            return data

        except Exception as e:
            print(f"Error in recognize_with_psm: {e}")
            return {}

    def recognize_to_string(self, image, psm: int = 6) -> str:
        """
        Runs Tesseract's plain text mode.

        TSV confidence filtering is useful for product cards, but it is too
        aggressive for mixed-script OCR because CJK tokens often get low or
        differently shaped confidence data. Plain text mode keeps those tokens.
        """
        try:
            import pytesseract

            config = f"--oem 1 --psm {psm}"
            text = pytesseract.image_to_string(
                image,
                lang=self.ocr_language_string(),
                config=config,
            )
            return text.strip()
        except Exception as e:
            print(f"Error in recognize_to_string: {e}")
            return ""

    def recognize(self, image, mode: str = "text_mode", psm: int = 6) -> str:
        """
        Main recognition method.
        Uses TSV output with confidence filtering for cleaner results.
        """
        try:
            if mode == "text_mode":
                text = self.recognize_to_string(image, psm)
                if text:
                    return text

            data = self.recognize_with_psm(image, psm, mode)

            if not data:
                return ""

            # Build text from TSV with confidence filtering
            text = self.build_text_from_tsv(data, min_conf=40)

            return text

        except Exception as e:
            return f"Tesseract error: {str(e)}"

    def recognize_card(self, image) -> dict:
        """
        Card-aware OCR: processes a product card image.
        Crops garbage zones, uses appropriate PSM modes for different parts.

        Returns dict with:
            - full_text: OCR of center text area (PSM 4/6)
            - price: extracted price (PSM 7)
            - quantity: extracted quantity (PSM 7)
            - raw_tsv: raw TSV data for further processing
        """
        # Crop garbage zones (remove image and buttons)
        cropped = self.crop_garbage_zones(image, left_percent=0.15, right_percent=0.20)

        result = {
            "full_text": "",
            "price": "",
            "quantity": "",
            "raw_tsv": {},
        }

        # OCR the text area with PSM 4 (single column of variable text)
        data = self.recognize_with_psm(cropped, psm=4, mode="card")

        if data:
            result["raw_tsv"] = data
            result["full_text"] = self.build_text_from_tsv(data, min_conf=40)

            # Try to extract price using PSM 7 on specific regions
            # Look for price patterns in the full text
            price_pattern = r"(\d+[.,]?\d*)\s*(₽|р|руб|\$|€)"
            prices = re.findall(price_pattern, result["full_text"])
            if prices:
                result["price"] = f"{prices[-1][0]} {prices[-1][1]}"

            # Look for quantity patterns
            qty_pattern = r"(\d+)\s*(шт|г|кг|мл|л)"
            quantities = re.findall(qty_pattern, result["full_text"])
            if quantities:
                result["quantity"] = f"{quantities[0][0]} {quantities[0][1]}"

        return result

    def available(self) -> bool:
        try:
            import pytesseract

            pytesseract.get_tesseract_version()
            return True
        except Exception:
            return False

    def info(self) -> dict:
        return {
            "engine": "tesseract",
            "device": "cpu",
            "langs": self.ocr_language_string(),
            "installed_langs": self.installed_languages(),
        }
