from pathlib import Path
from app.loaders.base import DocumentLoader

class ImageLoader(DocumentLoader):
    def load(self, path: Path) -> list:
        try:
            from PIL import Image
            img = Image.open(path)
            return [img]
        except Exception as e:
            # Fallback returning path if PIL not fully available or error
            return [path]
