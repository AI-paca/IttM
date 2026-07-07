import re
from contextlib import contextmanager

import numpy as np
from PIL import Image, ImageOps

from app.engines.base import OcrEngine
from app.recognition.candidates import LanguageCandidateSelector
from app.recognition.languages import (
    BASE_LANGUAGES,
    ISOLATED_LANGUAGES,
    LANGUAGE_SCRIPTS,
    OPTIONAL_LANGUAGES,
    SCRIPT_PATTERNS,
    T9_RETRY_LANGUAGES,
    normalize_probabilities,
    ocr_language_string_for,
    script_counts,
)
from app.recognition.quality import (
    bbox_overlap_ratio,
    merge_language_word_candidates,
    should_retry_word_languages,
    text_noise_ratio,
    text_quality_score,
    word_candidate_quality,
    word_score,
    words_to_text,
)


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
    BASE_LANGUAGES = BASE_LANGUAGES
    OPTIONAL_LANGUAGES = OPTIONAL_LANGUAGES
    T9_RETRY_LANGUAGES = T9_RETRY_LANGUAGES
    ISOLATED_LANGUAGES = ISOLATED_LANGUAGES
    LANGUAGE_SCRIPTS = LANGUAGE_SCRIPTS
    SCRIPT_PATTERNS = SCRIPT_PATTERNS
    OCR_BORDER_PIXELS = 10

    def __init__(
        self,
        language_priority: tuple[str, ...] | None = None,
        ocr_border_pixels: int = OCR_BORDER_PIXELS,
        edge_word_fallback_psms: tuple[int, ...] = (8, 13),
        language_retry: str = "off",
    ):
        self.language_priority = language_priority
        self.ocr_border_pixels = ocr_border_pixels
        self.edge_word_fallback_psms = edge_word_fallback_psms
        self.language_retry = language_retry
        self.language_phase = LanguageCandidateSelector(
            language_priority=language_priority,
            language_retry=language_retry,
            installed_languages=self.installed_languages,
        )
        self._language_context: tuple[str, ...] = ()

    @staticmethod
    def installed_languages() -> list:
        try:
            import pytesseract

            return pytesseract.get_languages(config="")
        except Exception:
            return []

    @classmethod
    def ocr_language_string(cls) -> str:
        return cls.ocr_language_string_for(cls.BASE_LANGUAGES + cls.OPTIONAL_LANGUAGES)

    @classmethod
    def ocr_language_string_for(cls, language_priority: tuple[str, ...]) -> str:
        return ocr_language_string_for(language_priority, cls.installed_languages())

    def configured_ocr_language_string(self) -> str:
        return self.language_phase.configured_ocr_language_string()

    def _single_language_candidates(self) -> tuple[str, ...]:
        return self.language_phase.single_language_candidates()

    def _initial_language_probabilities(self) -> dict[str, float]:
        return self.language_phase.initial_language_probabilities()

    def language_probabilities(self) -> dict[str, float]:
        return self.language_phase.language_probabilities()

    @classmethod
    def _script_counts(cls, text: str) -> dict[str, int]:
        return script_counts(text)

    @classmethod
    def _language_script(cls, language: str) -> str:
        return cls.LANGUAGE_SCRIPTS.get(language, "latin")

    @staticmethod
    def _normalize_probabilities(probabilities: dict[str, float]) -> dict[str, float]:
        return normalize_probabilities(probabilities)

    def _language_evidence(self, text: str) -> dict[str, float]:
        return self.language_phase.language_evidence(text)

    def _update_language_probabilities(self, text: str) -> None:
        self.language_phase.update_language_probabilities(text)

    def _observe_language_candidate(self, lang: str | None, text: str) -> None:
        self.language_phase.observe_language_candidate(lang, text)

    def _ranked_single_language_candidates(self) -> tuple[str, ...]:
        return self.language_phase.ranked_single_language_candidates(
            self._language_context,
        )

    def _language_candidates(self) -> tuple[str, ...]:
        return self.language_phase.language_candidates(
            self._language_context,
        )

    @contextmanager
    def language_context(self, *parts: str):
        previous = self._language_context
        self._language_context = tuple(part for part in parts if part)
        try:
            yield
        finally:
            self._language_context = previous

    def _add_ocr_border(self, image: Image.Image) -> Image.Image:
        return ImageOps.expand(image, border=self.ocr_border_pixels, fill="white")

    @staticmethod
    def _ink_ratio(image: Image.Image) -> float:
        gray = image.convert("L")
        try:
            histogram = gray.histogram()
        finally:
            if gray is not image:
                gray.close()
        total = sum(histogram)
        if total <= 0:
            return 0.0
        return sum(histogram[:220]) / total

    @classmethod
    def _edge_ink_touches_all_sides(cls, image: Image.Image) -> bool:
        width, height = image.size
        if width < 80 or height < 40:
            return False

        edge = max(2, min(12, min(width, height) // 80))
        strips = (
            image.crop((0, 0, width, edge)),
            image.crop((0, height - edge, width, height)),
            image.crop((0, 0, edge, height)),
            image.crop((width - edge, 0, width, height)),
        )
        try:
            return all(cls._ink_ratio(strip) >= 0.02 for strip in strips)
        finally:
            for strip in strips:
                strip.close()

    @staticmethod
    def _parse_confidence(conf) -> float:
        try:
            return float(conf)
        except (TypeError, ValueError):
            return -1.0

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
            conf = TesseractEngine._parse_confidence(data["conf"][i])
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

    def recognize_with_psm(
        self,
        image,
        psm: int = 6,
        mode: str = "text_mode",
        lang: str | None = None,
    ) -> dict:
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

            data = pytesseract.image_to_data(
                image,  # Pass original image directly
                lang=lang or self.configured_ocr_language_string(),
                config=config,
                output_type=pytesseract.Output.DICT,
            )
            return data

        except Exception as e:
            print(f"Error in recognize_with_psm: {e}")
            return {}

    def recognize_to_string(
        self,
        image,
        psm: int = 6,
        lang: str | None = None,
    ) -> str:
        """
        Runs Tesseract's plain text mode.

        TSV confidence filtering is useful for product cards, but it is too
        aggressive for mixed-script OCR because CJK tokens often get low or
        differently shaped confidence data. Plain text mode keeps those tokens.
        """
        try:
            import pytesseract

            config = f"--oem 1 --psm {psm}"
            bordered = self._add_ocr_border(image)
            text = pytesseract.image_to_string(
                bordered,
                lang=lang or self.configured_ocr_language_string(),
                config=config,
            )
            return text.strip()
        except Exception as e:
            print(f"Error in recognize_to_string: {e}")
            return ""
        finally:
            if "bordered" in locals():
                bordered.close()

    @staticmethod
    def _text_quality_score(text: str) -> float:
        return text_quality_score(text)

    @staticmethod
    def _text_noise_ratio(text: str) -> float:
        return text_noise_ratio(text)

    @staticmethod
    def _word_candidate_quality(words: list[dict]) -> float:
        return word_candidate_quality(words)

    @classmethod
    def _should_retry_word_languages(cls, words: list[dict]) -> bool:
        return should_retry_word_languages(words)

    def _candidate_prior_score(self, lang: str | None) -> float:
        return self.language_phase.candidate_prior_score(
            lang,
            self._language_context,
        )

    def _candidate_evidence_score(self, lang: str | None, text: str) -> float:
        return self.language_phase.candidate_evidence_score(lang, text)

    @classmethod
    def _script_candidate_penalty(cls, lang: str | None, text: str) -> float:
        return LanguageCandidateSelector.script_candidate_penalty(lang, text)

    def _text_candidate_score(self, text: str, lang: str | None) -> float:
        return self.language_phase.text_candidate_score(
            text,
            lang,
            self._language_context,
        )

    def _select_text_candidate(
        self,
        candidates: list[tuple[str, str]],
    ) -> tuple[str, str]:
        return self.language_phase.select_text_candidate(
            candidates,
            self._language_context,
        )

    @staticmethod
    def _words_to_text(words: list[dict]) -> str:
        return words_to_text(words)

    def _select_word_candidates(
        self,
        candidates: list[tuple[str, list[dict]]],
    ) -> list[dict]:
        return self.language_phase.select_word_candidates(
            candidates,
            self._language_context,
        )

    def _select_word_candidate(
        self,
        candidates: list[tuple[str, list[dict]]],
    ) -> tuple[str, list[dict]]:
        return self.language_phase.select_word_candidate(
            candidates,
            self._language_context,
        )

    @staticmethod
    def _bbox_overlap_ratio(
        first: tuple[int, int, int, int],
        second: tuple[int, int, int, int],
    ) -> float:
        return bbox_overlap_ratio(first, second)

    @classmethod
    def _word_score(cls, word: dict) -> float:
        return word_score(word)

    @classmethod
    def _merge_language_word_candidates(
        cls,
        candidates: list[list[dict]],
    ) -> list[dict]:
        return merge_language_word_candidates(candidates)

    def _words_from_data(self, data: dict, min_conf: int) -> list[dict]:
        if not data:
            return []

        words = []
        for index, raw_text in enumerate(data.get("text", [])):
            text = raw_text.strip()
            if not text:
                continue

            conf = self._parse_confidence(data["conf"][index])
            if conf == -1:
                continue
            if conf < min_conf and not re.match(r"^\d+[.,]?\d*$", text):
                continue

            left = int(data["left"][index])
            top = int(data["top"][index])
            width = int(data["width"][index])
            height = int(data["height"][index])
            if width <= 0 or height <= 0:
                continue

            words.append(
                {
                    "text": text,
                    "bbox": (left, top, left + width, top + height),
                    "conf": conf,
                }
            )

        return words

    def recognize_words(self, image, psm: int = 6, min_conf: int = 20) -> list[dict]:
        language_candidates = self._language_candidates()
        primary_data = self.recognize_with_psm(
            image,
            psm=psm,
            mode="table",
            lang=language_candidates[0],
        )
        primary_words = self._words_from_data(primary_data, min_conf)
        if self.language_retry != "t9_small" or not self._should_retry_word_languages(primary_words):
            if primary_words:
                self._observe_language_candidate(
                    language_candidates[0],
                    " ".join(str(word.get("text", "")) for word in primary_words),
                )
            return primary_words

        candidates = [(language_candidates[0], primary_words)] if primary_words else []
        for lang in language_candidates[1:]:
            data = self.recognize_with_psm(image, psm=psm, mode="table", lang=lang)
            words = self._words_from_data(data, min_conf)
            if words:
                candidates.append((lang, words))
        if not candidates:
            return []
        if len(candidates) == 1:
            selected_lang, words = candidates[0]
        else:
            selected_lang, words = self._select_word_candidate(candidates)
        self._observe_language_candidate(selected_lang, self._words_to_text(words))
        return words

    def recognize_words_for_language(
        self,
        image,
        language: str,
        psm: int = 6,
        min_conf: int = 20,
    ) -> list[dict]:
        if language not in self.installed_languages():
            return []
        data = self.recognize_with_psm(
            image,
            psm=psm,
            mode="table",
            lang=language,
        )
        return self._words_from_data(data, min_conf)

    def recognize(self, image, mode: str = "text_mode", psm: int = 6) -> str:
        """
        Main recognition method.
        Uses TSV output with confidence filtering for cleaner results.
        """
        try:
            if mode == "text_mode":
                text_candidates = [
                    (lang, self.recognize_to_string(image, psm, lang=lang)) for lang in self._language_candidates()
                ]
                selected_lang, text = self._select_text_candidate(text_candidates)
                if text:
                    self._observe_language_candidate(selected_lang, text)
                    return text
                if psm not in self.edge_word_fallback_psms and self._edge_ink_touches_all_sides(image):
                    for fallback_psm in self.edge_word_fallback_psms:
                        text_candidates = [
                            (
                                lang,
                                self.recognize_to_string(
                                    image,
                                    fallback_psm,
                                    lang=lang,
                                ),
                            )
                            for lang in self._language_candidates()
                        ]
                        selected_lang, text = self._select_text_candidate(text_candidates)
                        if text:
                            self._observe_language_candidate(selected_lang, text)
                            return text

            data_candidates = [
                (
                    lang,
                    self.recognize_with_psm(
                        image,
                        psm,
                        mode,
                        lang=lang,
                    ),
                )
                for lang in self._language_candidates()
            ]
            data_texts = [(lang, self.build_text_from_tsv(data, min_conf=40)) for lang, data in data_candidates if data]
            if data_texts:
                selected_lang, text = self._select_text_candidate(data_texts)
                if text:
                    self._observe_language_candidate(selected_lang, text)
                    return text

            return ""

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

        if result["full_text"]:
            self._update_language_probabilities(result["full_text"])

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
            "langs": self.configured_ocr_language_string(),
            "installed_langs": self.installed_languages(),
            "language_retry": self.language_retry,
            "language_probabilities": self.language_probabilities(),
        }
