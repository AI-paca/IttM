from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from app.sparse_pipeline.ocr_adapter_contracts import OcrFailureCode
from app.sparse_pipeline.ocr_adapters import (
    EasyOcrConfig,
    EasyOcrWorker,
    GlmOcrConfig,
    GlmOcrWorker,
)
from app.sparse_pipeline.ocr_queue import OcrEngineOutput
from app.sparse_pipeline.ocr_rpc import _receive_frame, _send_frame


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--engine", choices=("easyocr", "glm_ocr"), required=True)
    parser.add_argument("--config-json", required=True)
    return parser.parse_args()


def _make_worker(engine: str, config: dict[str, object]) -> object:
    if engine == "easyocr":
        return EasyOcrWorker(
            EasyOcrConfig(
                languages=tuple(str(value) for value in config["languages"]),
                model_storage_directory=Path(str(config["model_storage_directory"])),
                gpu=bool(config["gpu"]),
                download_enabled=False,
                decoder=str(config["decoder"]),
                batch_size=int(config["batch_size"]),
            )
        )
    return GlmOcrWorker(
        GlmOcrConfig(
            model_directory=Path(str(config["model_directory"])),
            device=str(config["device"]),
            dtype=str(config["dtype"]),
            prompt=str(config["prompt"]),
            max_new_tokens=int(config["max_new_tokens"]),
        )
    )


def _error_header(exc: Exception, *, request_id: object = None) -> dict[str, object]:
    code = getattr(exc, "code", OcrFailureCode.ENGINE_ERROR)
    if not isinstance(code, OcrFailureCode):
        code = OcrFailureCode.ENGINE_ERROR
    return {
        "type": "error",
        "request_id": request_id,
        "failure_code": code.value,
        "error_type": type(exc).__name__,
        "message": " ".join(str(exc).split())[:512],
    }


def _result_header(
    output: OcrEngineOutput,
    *,
    request_id: object,
    payload_sha256: str,
) -> dict[str, object]:
    return {
        "type": "result",
        "request_id": request_id,
        "payload_sha256": payload_sha256,
        "geometry": output.geometry.value,
        "text": output.text,
        "words": [
            {
                "text": word.text,
                "bbox": list(word.bbox.as_tuple()),
                "confidence": float(word.confidence),
            }
            for word in output.words
        ],
    }


def main() -> int:
    args = _parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    protocol_output = os.fdopen(
        os.dup(sys.stdout.fileno()),
        "wb",
        buffering=0,
    )
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    protocol_input = sys.stdin.buffer
    try:
        try:
            raw_config = json.loads(args.config_json)
            if not isinstance(raw_config, dict):
                raise ValueError("OCR RPC config must be a JSON object")
            worker = _make_worker(args.engine, raw_config)
        except Exception as exc:
            _send_frame(protocol_output, _error_header(exc), b"")
            return 2
        _send_frame(
            protocol_output,
            {"type": "ready", "engine": args.engine},
            b"",
        )
        while True:
            try:
                request, payload = _receive_frame(
                    protocol_input,
                    timeout=24 * 60 * 60,
                )
            except (EOFError, OSError):
                return 0
            request_type = request.get("type")
            if request_type == "close":
                _send_frame(protocol_output, {"type": "closed"}, b"")
                return 0
            request_id = request.get("request_id")
            digest = hashlib.sha256(payload).hexdigest()
            if (
                request_type != "recognize"
                or type(request_id) is not int
                or request_id < 1
                or request.get("payload_sha256") != digest
            ):
                error = _error_header(
                    ValueError("OCR RPC request metadata is invalid"),
                    request_id=request_id,
                )
                error["failure_code"] = OcrFailureCode.PROTOCOL_ERROR.value
                error["payload_sha256"] = digest
                _send_frame(protocol_output, error, b"")
                return 3
            poisoned = False
            try:
                output = worker.recognize(payload)
                if not isinstance(output, OcrEngineOutput):
                    raise TypeError("OCR worker returned an invalid result type")
                response = _result_header(
                    output,
                    request_id=request_id,
                    payload_sha256=digest,
                )
            except Exception as exc:
                response = _error_header(exc, request_id=request_id)
                response["payload_sha256"] = digest
                poisoned = bool(getattr(exc, "worker_poisoned", False))
            _send_frame(protocol_output, response, b"")
            if poisoned:
                return 4
    finally:
        close = getattr(locals().get("worker"), "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        protocol_output.close()


if __name__ == "__main__":
    raise SystemExit(main())
