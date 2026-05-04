from pathlib import Path
from app.loaders.base import DocumentLoader


class PdfLoader(DocumentLoader):
    def load(self, path: Path) -> list:
        """
        Load PDF pages as images with DPI=300 for better OCR quality.
        Returns a list of PIL Image objects.
        """
        try:
            from pdf2image import convert_from_path
            
            # DPI=300 for better OCR quality (not 200 which is too low)
            images = convert_from_path(str(path), dpi=300, fmt='RGB')
            return images
        except ImportError:
            # pdf2image not installed
            return []
        except Exception as e:
            print(f"Error loading PDF: {e}")
            return []
