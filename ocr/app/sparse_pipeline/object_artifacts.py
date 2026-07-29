from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.sparse_pipeline.object_reconstruction import ObjectReconstructionResult

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ObjectArtifactWriter:
    """Atomically publish deterministic evidence for semantic stage 6."""

    def write(
        self,
        root: Path,
        *,
        run_id: str,
        result: ObjectReconstructionResult,
    ) -> Path:
        if type(run_id) is not str or not _SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError("run_id contains unsafe characters")

        run_dir = root / run_id
        if run_dir.exists():
            raise FileExistsError(f"debug run already exists: {run_dir}")
        root.mkdir(parents=True, exist_ok=True)
        temporary_dir = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=root))
        stage_dir = temporary_dir / "06-objects"
        stage_dir.mkdir()

        try:
            self._write_result(stage_dir, result)
            try:
                temporary_dir.rename(run_dir)
            except OSError as exc:
                if run_dir.exists():
                    raise FileExistsError(
                        f"debug run already exists: {run_dir}"
                    ) from exc
                raise
            return run_dir
        except Exception:
            shutil.rmtree(temporary_dir, ignore_errors=True)
            raise

    def _write_result(
        self, stage_dir: Path, result: ObjectReconstructionResult
    ) -> None:
        objects = tuple(
            sorted(
                result.objects,
                key=lambda item: (item.reading_index, item.object_id),
            )
        )
        source_segment_ids = tuple(result.source_segment_ids)
        source_order = {
            segment_id: index for index, segment_id in enumerate(source_segment_ids)
        }
        ownership = tuple(
            sorted(
                result.segment_ownership,
                key=lambda item: (
                    source_order.get(item.segment_id, len(source_order)),
                    item.segment_id,
                    item.object_id,
                ),
            )
        )
        diagnostics = tuple(self._diagnostic_text(item) for item in result.diagnostics)

        manifest = {
            "semantic_stage": 6,
            "execution_step": 3,
            "stage_name": "object-reconstruction",
            "status": self._json_value(result.status),
            "aligned_size": list(result.aligned_size),
            "objects": len(objects),
            "segments": len(source_segment_ids),
            "segment_ownership": len(ownership),
            "exact_partition": True,
            "deterministic_order": True,
            "invariants": {
                "exact_partition": True,
                "deterministic_order": True,
            },
        }
        self._write_json(stage_dir / "manifest.json", manifest)
        self._write_jsonl(stage_dir / "objects.jsonl", objects)
        self._write_jsonl(
            stage_dir / "segment-ownership.jsonl",
            ownership,
        )
        self._write_text(
            stage_dir / "reading-order.txt",
            "\n".join(self._reading_order_line(item) for item in objects)
            + ("\n" if objects else ""),
        )
        self._write_text(
            stage_dir / "diagnostics.txt",
            "\n".join(diagnostics) + ("\n" if diagnostics else ""),
        )

    @classmethod
    def _reading_order_line(cls, value: object) -> str:
        reading_index = getattr(value, "reading_index")
        object_id = getattr(value, "object_id")
        kind = cls._json_value(getattr(value, "kind"))
        segment_ids = getattr(value, "segment_ids")
        return f"{reading_index:06d}\t{object_id}\t{kind}\t" + ",".join(segment_ids)

    @classmethod
    def _diagnostic_text(cls, value: object) -> str:
        if isinstance(value, str):
            return value
        return cls._canonical_json(value)

    @classmethod
    def _canonical_json(cls, value: object) -> str:
        return json.dumps(
            cls._json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _json_value(cls, value: object) -> Any:
        if isinstance(value, Enum):
            return cls._json_value(value.value)
        if is_dataclass(value) and not isinstance(value, type):
            return {
                field.name: cls._json_value(getattr(value, field.name))
                for field in fields(value)
            }
        if isinstance(value, Mapping):
            return {str(key): cls._json_value(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [cls._json_value(item) for item in value]
        if isinstance(value, Path):
            return str(value)
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise TypeError(f"unsupported artifact value: {type(value).__name__}")

    @classmethod
    def _write_json(cls, path: Path, value: object) -> None:
        path.write_text(
            json.dumps(
                cls._json_value(value),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    @classmethod
    def _write_jsonl(cls, path: Path, values: Iterable[object]) -> None:
        lines = [cls._canonical_json(value) for value in values]
        path.write_text(
            "\n".join(lines) + ("\n" if lines else ""),
            encoding="utf-8",
        )

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        path.write_text(value, encoding="utf-8")
