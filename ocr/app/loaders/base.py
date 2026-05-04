from pathlib import Path
from typing import List

class DocumentLoader:
    def load(self, path: Path) -> List[Any]:
        raise NotImplementedError
