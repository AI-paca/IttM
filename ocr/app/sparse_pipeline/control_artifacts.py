from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from app.sparse_pipeline.recursive_control import RunOutcome

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ControlArtifactWriter:
    """Writes deterministic stage-3 TXT/JSONL evidence into a fresh run dir."""

    def write(self, root: Path, *, run_id: str, outcome: RunOutcome) -> Path:
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError("run_id contains unsafe characters")

        run_dir = root / run_id
        if run_dir.exists():
            raise FileExistsError(f"debug run already exists: {run_dir}")
        root.mkdir(parents=True, exist_ok=True)
        temporary_dir = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=root))
        stage_dir = temporary_dir / "00-control"
        stage_dir.mkdir()

        try:
            manifest = {
                "stage": 3,
                "stage_name": "recursive-control",
                "status": outcome.status.value,
                "steps": outcome.steps,
                "derivations": len(outcome.derivations),
                "evidence_atoms": len(outcome.evidence),
                "trace_events": len(outcome.trace),
                "root_result_id": outcome.root.result_id if outcome.root is not None else None,
                "unresolved_node_ids": list(outcome.unresolved_node_ids),
                "error": outcome.error,
            }
            self._write_json(stage_dir / "manifest.json", manifest)
            evidence_values = tuple(asdict(atom) for atom in outcome.evidence)
            self._write_jsonl(stage_dir / "evidence.jsonl", evidence_values)
            self._write_jsonl(stage_dir / "trace.jsonl", (asdict(event) for event in outcome.trace))
            self._write_jsonl(
                stage_dir / "derivations.jsonl",
                (asdict(item) for item in outcome.derivations),
            )

            evidence_lines = [
                f"{atom.atom_id}\t{atom.source_id}\t{atom.order_key!r}\t{atom.payload!r}" for atom in outcome.evidence
            ]
            self._write_text(
                stage_dir / "evidence.txt",
                "\n".join(evidence_lines) + ("\n" if evidence_lines else ""),
            )

            trace_lines = []
            for event in outcome.trace:
                details = " ".join(f"{key}={value}" for key, value in event.details)
                trace_lines.append(f"{event.step:06d} {event.event:<8} {event.node_id} {details}".rstrip())
            self._write_text(stage_dir / "trace.txt", "\n".join(trace_lines) + "\n")

            if outcome.root is not None:
                result_lines = [
                    f"result_id={outcome.root.result_id}",
                    f"node_id={outcome.root.node_id}",
                    f"kind={outcome.root.kind}",
                    f"fallback={str(outcome.root.fallback).lower()}",
                    "atom_ids=" + ",".join(outcome.root.atom_ids),
                ]
                self._write_text(stage_dir / "result.txt", "\n".join(result_lines) + "\n")
            if outcome.error is not None:
                self._write_text(stage_dir / "error.txt", outcome.error + "\n")
                self._write_json(
                    stage_dir / "error.json",
                    {
                        "error": outcome.error,
                        "status": outcome.status.value,
                        "unresolved_node_ids": list(outcome.unresolved_node_ids),
                    },
                )

            try:
                temporary_dir.rename(run_dir)
            except OSError as exc:
                if run_dir.exists():
                    raise FileExistsError(f"debug run already exists: {run_dir}") from exc
                raise
            return run_dir
        except Exception:
            shutil.rmtree(temporary_dir, ignore_errors=True)
            raise

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _write_jsonl(path: Path, values: object) -> None:
        lines = [json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for value in values]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        path.write_text(value, encoding="utf-8")
