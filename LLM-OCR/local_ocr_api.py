#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from typing import Any


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "to_dict"):
        return _jsonable(value.to_dict())
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


class PaddleBackend:
    def __init__(self) -> None:
        from paddleocr import PaddleOCR

        self.ocr = PaddleOCR(
            text_detection_model_name=os.environ.get(
                "PADDLE_OCR_DET_MODEL", "PP-OCRv6_medium_det"
            ),
            text_recognition_model_name=os.environ.get(
                "PADDLE_OCR_REC_MODEL", "PP-OCRv6_medium_rec"
            ),
            engine="transformers",
            use_doc_orientation_classify=os.environ.get(
                "PADDLE_OCR_USE_DOC_ORIENTATION", "false"
            ).casefold()
            in {"1", "true", "yes", "on"},
            use_doc_unwarping=os.environ.get(
                "PADDLE_OCR_USE_DOC_UNWARPING", "false"
            ).casefold()
            in {"1", "true", "yes", "on"},
            use_textline_orientation=os.environ.get(
                "PADDLE_OCR_USE_TEXTLINE_ORIENTATION", "true"
            ).casefold()
            in {"1", "true", "yes", "on"},
        )

    def predict(self, image_path: Path, prompt: str) -> dict[str, Any]:
        results = self.ocr.predict(str(image_path))
        return {
            "backend": "paddle",
            "prompt": prompt,
            "items": [_jsonable(result) for result in results],
            "text": "\n".join(str(result) for result in results),
        }


class NemotronBackend:
    def __init__(self) -> None:
        from nemotron_ocr.inference.pipeline_v2 import NemotronOCRV2

        kwargs: dict[str, Any] = {}
        model_dir = os.environ.get("NEMOTRON_OCR_MODEL_DIR")
        if model_dir:
            kwargs["model_dir"] = model_dir
        else:
            kwargs["lang"] = os.environ.get("NEMOTRON_OCR_LANG", "multi")
        if os.environ.get("NEMOTRON_OCR_DETECTOR_ONLY", "").casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            kwargs["detector_only"] = True
        if os.environ.get("NEMOTRON_OCR_SKIP_RELATIONAL", "").casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            kwargs["skip_relational"] = True
        self.ocr = NemotronOCRV2(**kwargs)

    def predict(self, image_path: Path, prompt: str) -> dict[str, Any]:
        merge_level = os.environ.get("NEMOTRON_OCR_MERGE_LEVEL", "paragraph")
        predictions = self.ocr(str(image_path), merge_level=merge_level)
        return {
            "backend": "nemotron",
            "prompt": prompt,
            "merge_level": merge_level,
            "items": _jsonable(predictions),
            "text": "\n".join(str(item.get("text", "")) for item in predictions),
        }


def create_app(server_backend: str):
    from fastapi import FastAPI, File, Form, UploadFile

    app = FastAPI(title="Local LLM OCR API", version="0.1.0")
    backend_holder: dict[str, Any] = {}

    def backend():
        if "backend" not in backend_holder:
            if server_backend == "paddle":
                backend_holder["backend"] = PaddleBackend()
            elif server_backend == "nemotron":
                backend_holder["backend"] = NemotronBackend()
            else:
                raise ValueError(f"Unknown backend: {server_backend}")
        return backend_holder["backend"]

    @app.get("/health")
    def health():
        return {"ok": True, "backend": server_backend}

    @app.post("/v1/ocr")
    async def ocr(
        file: UploadFile = File(...),
        prompt: str = Form("Parse this document to Markdown."),
    ):
        suffix = Path(file.filename or "image.png").suffix or ".png"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            content = await file.read()
            handle.write(content)
            image_path = Path(handle.name)
        try:
            return backend().predict(image_path, prompt)
        finally:
            image_path.unlink(missing_ok=True)

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Small local OCR HTTP wrapper.")
    parser.add_argument("--backend", choices=["paddle", "nemotron"], required=True)
    parser.add_argument(
        "--host", default=os.environ.get("LLM_OCR_API_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("LLM_OCR_API_PORT", "18080"))
    )
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(create_app(args.backend), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
