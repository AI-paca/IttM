from __future__ import annotations

import hashlib
import json
import os
import select
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.sparse_pipeline.contracts import Box
from app.sparse_pipeline.ocr_adapter_contracts import (
    OcrAdapterError,
    OcrDependencyUnavailableError,
    OcrDeviceUnavailableError,
    OcrEngineExecutionError,
    OcrExecutableUnavailableError,
    OcrFailureCode,
    OcrInferenceTimeoutError,
    OcrInvalidEngineOutputError,
    OcrLanguageUnavailableError,
    OcrModelIntegrityError,
    OcrModelMissingError,
    OcrOfflinePolicyError,
    OcrOutputGeometry,
    OcrOutputTruncatedError,
    OcrRecognitionMissError,
    OcrResourceExhaustedError,
    OcrWorkerDiedError,
    OcrWorkerProtocolError,
)
from app.sparse_pipeline.ocr_queue import OcrEngineOutput, OcrWord

_HEADER_LIMIT = 4 * 1024 * 1024
_PAYLOAD_LIMIT = 128 * 1024 * 1024
_LOG_LIMIT = 32 * 1024


@dataclass(frozen=True)
class ExternalOcrSpec:
    python_executable: Path
    engine: str
    config: tuple[tuple[str, object], ...]
    startup_timeout_seconds: float = 180.0
    request_timeout_seconds: float = 180.0

    def __post_init__(self) -> None:
        if not isinstance(self.python_executable, Path):
            raise ValueError("external OCR Python executable must be a Path")
        if self.engine not in {"easyocr", "glm_ocr"}:
            raise ValueError("external OCR engine is unsupported")
        if type(self.config) is not tuple or any(
            type(item) is not tuple or len(item) != 2 or type(item[0]) is not str for item in self.config
        ):
            raise ValueError("external OCR config must be immutable key/value pairs")
        keys = tuple(item[0] for item in self.config)
        if len(keys) != len(set(keys)):
            raise ValueError("external OCR config keys must be unique")
        for value in (
            self.startup_timeout_seconds,
            self.request_timeout_seconds,
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 < float(value) <= 3_600.0:
                raise ValueError("external OCR timeouts must be positive and bounded")


class ExternalOcrWorker:
    """One strict OCR worker hosted by its exact Python environment."""

    def __init__(self, spec: ExternalOcrSpec) -> None:
        if not isinstance(spec, ExternalOcrSpec):
            raise TypeError("spec must be an ExternalOcrSpec")
        executable = spec.python_executable.expanduser()
        if not executable.is_absolute():
            executable = executable.absolute()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise OcrExecutableUnavailableError(f"external OCR Python is unavailable: {executable}")
        self.spec = spec
        self._lock = threading.Lock()
        self._request_id = 0
        self._closed = False
        self._logs = bytearray()
        package_root = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(package_root) if not existing_pythonpath else str(package_root) + os.pathsep + existing_pythonpath
        )
        environment["HF_HUB_OFFLINE"] = "1"
        environment["TRANSFORMERS_OFFLINE"] = "1"
        command = (
            str(executable),
            "-u",
            "-m",
            "app.sparse_pipeline.ocr_rpc_worker",
            "--engine",
            spec.engine,
            "--config-json",
            json.dumps(
                dict(spec.config),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=environment,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise OcrExecutableUnavailableError("external OCR process could not start") from exc
        if self._process.stdin is None or self._process.stdout is None or self._process.stderr is None:
            self._process.kill()
            raise OcrWorkerProtocolError("external OCR pipes are unavailable")
        self._writer = self._process.stdin
        self._reader = self._process.stdout
        self._log_thread = threading.Thread(
            target=self._drain_logs,
            name=f"ocr-rpc-log-{self._process.pid}",
            daemon=True,
        )
        self._log_thread.start()
        try:
            ready, payload = _receive_frame(
                self._reader,
                timeout=float(spec.startup_timeout_seconds),
            )
            if payload or ready.get("type") not in {"ready", "error"}:
                raise OcrWorkerProtocolError("external OCR startup handshake is malformed")
            if ready.get("type") == "error":
                raise _remote_error(ready, self.bounded_logs)
            if ready.get("engine") != spec.engine:
                raise OcrWorkerProtocolError("external OCR startup engine identity disagrees")
        except (socket.timeout, TimeoutError) as exc:
            self._terminate()
            raise OcrInferenceTimeoutError("external OCR startup timed out") from exc
        except (EOFError, OSError) as exc:
            try:
                self._process.wait(timeout=2.0)
                self._log_thread.join(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
            logs = self.bounded_logs
            self._terminate()
            raise OcrWorkerDiedError(f"external OCR worker exited during startup: {logs}") from exc
        except Exception:
            self._terminate()
            raise

    @property
    def bounded_logs(self) -> str:
        return bytes(self._logs).decode("utf-8", errors="replace")

    def recognize(self, png_bytes: bytes) -> OcrEngineOutput:
        if type(png_bytes) is not bytes or not png_bytes:
            raise OcrInvalidEngineOutputError("external OCR input must be non-empty immutable bytes")
        if len(png_bytes) > _PAYLOAD_LIMIT:
            raise OcrInvalidEngineOutputError("external OCR input exceeds its protocol payload limit")
        with self._lock:
            if self._closed:
                raise OcrWorkerDiedError("external OCR worker is closed")
            if self._process.poll() is not None:
                self._closed = True
                raise OcrWorkerDiedError(f"external OCR worker exited: {self.bounded_logs}")
            self._request_id += 1
            request_id = self._request_id
            digest = hashlib.sha256(png_bytes).hexdigest()
            try:
                _send_frame(
                    self._writer,
                    {
                        "type": "recognize",
                        "request_id": request_id,
                        "payload_sha256": digest,
                    },
                    png_bytes,
                )
                response, payload = _receive_frame(
                    self._reader,
                    timeout=float(self.spec.request_timeout_seconds),
                )
            except (socket.timeout, TimeoutError) as exc:
                self._terminate()
                raise OcrInferenceTimeoutError("external OCR request timed out") from exc
            except (BrokenPipeError, ConnectionError, EOFError, OSError) as exc:
                self._terminate()
                raise OcrWorkerDiedError(f"external OCR worker disconnected: {self.bounded_logs}") from exc
            if payload:
                self._terminate()
                raise OcrWorkerProtocolError("external OCR response unexpectedly contains binary payload")
            if type(response.get("request_id")) is not int or response.get("request_id") != request_id:
                self._terminate()
                raise OcrWorkerProtocolError("external OCR response request ID disagrees")
            if response.get("payload_sha256") != digest:
                self._terminate()
                raise OcrWorkerProtocolError("external OCR response payload digest disagrees")
            if response.get("type") == "error":
                error = _remote_error(response, self.bounded_logs)
                if error.worker_poisoned:
                    self._terminate()
                raise error
            if response.get("type") != "result":
                self._terminate()
                raise OcrWorkerProtocolError("external OCR response type is invalid")
            try:
                raw_geometry = response["geometry"]
                raw_text = response["text"]
                raw_words = response["words"]
                if type(raw_geometry) is not str or type(raw_text) is not str:
                    raise TypeError("OCR RPC result text/geometry types are invalid")
                if type(raw_words) is not list:
                    raise TypeError("OCR RPC result words must be a list")
                parsed_words: list[OcrWord] = []
                for item in raw_words:
                    if type(item) is not dict:
                        raise TypeError("OCR RPC word must be an object")
                    raw_word_text = item["text"]
                    raw_bbox = item["bbox"]
                    raw_confidence = item["confidence"]
                    if type(raw_word_text) is not str:
                        raise TypeError("OCR RPC word text must be a string")
                    if (
                        type(raw_bbox) is not list
                        or len(raw_bbox) != 4
                        or any(type(value) is not int for value in raw_bbox)
                    ):
                        raise TypeError("OCR RPC word bbox must contain four integers")
                    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
                        raise TypeError("OCR RPC word confidence must be numeric")
                    parsed_words.append(
                        OcrWord(
                            text=raw_word_text,
                            bbox=Box(*raw_bbox),
                            confidence=raw_confidence,
                        )
                    )
                geometry = OcrOutputGeometry(raw_geometry)
                return OcrEngineOutput(
                    text=raw_text,
                    words=tuple(parsed_words),
                    geometry=geometry,
                )
            except (KeyError, TypeError, ValueError) as exc:
                self._terminate()
                raise OcrWorkerProtocolError("external OCR result schema is invalid") from exc

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                _send_frame(self._writer, {"type": "close"}, b"")
                _receive_frame(self._reader, timeout=2.0)
            except Exception:
                pass
            finally:
                self._terminate()

    def _terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
        except (OSError, ValueError):
            pass
        try:
            self._reader.close()
        except (OSError, ValueError):
            pass
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3.0)

    def _drain_logs(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        while True:
            try:
                chunk = os.read(stream.fileno(), 4_096)
            except OSError:
                return
            if not chunk:
                return
            self._logs.extend(chunk)
            if len(self._logs) > _LOG_LIMIT:
                del self._logs[: len(self._logs) - _LOG_LIMIT]


def _send_frame(
    connection: object,
    header: dict[str, object],
    payload: bytes,
) -> None:
    if len(payload) > _PAYLOAD_LIMIT:
        raise OcrWorkerProtocolError("OCR RPC payload exceeds its limit")
    value = dict(header)
    value["payload_length"] = len(payload)
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _HEADER_LIMIT:
        raise OcrWorkerProtocolError("OCR RPC header exceeds its limit")
    framed = struct.pack(">I", len(encoded)) + encoded + payload
    sendall = getattr(connection, "sendall", None)
    if callable(sendall):
        sendall(framed)
        return
    write = getattr(connection, "write", None)
    flush = getattr(connection, "flush", None)
    if not callable(write) or not callable(flush):
        raise OcrWorkerProtocolError("OCR RPC writer is invalid")
    write(framed)
    flush()


def _receive_frame(
    connection: object,
    *,
    timeout: float,
) -> tuple[dict[str, object], bytes]:
    deadline = time.monotonic() + timeout
    header_size = struct.unpack(">I", _read_exact(connection, 4, deadline=deadline))[0]
    if header_size < 2 or header_size > _HEADER_LIMIT:
        raise OcrWorkerProtocolError("OCR RPC header size is invalid")
    try:
        header = json.loads(_read_exact(connection, header_size, deadline=deadline).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OcrWorkerProtocolError("OCR RPC header is invalid JSON") from exc
    if not isinstance(header, dict):
        raise OcrWorkerProtocolError("OCR RPC header must be an object")
    payload_length = header.get("payload_length")
    if type(payload_length) is not int or payload_length < 0 or payload_length > _PAYLOAD_LIMIT:
        raise OcrWorkerProtocolError("OCR RPC payload size is invalid")
    return header, _read_exact(connection, payload_length, deadline=deadline)


def _read_exact(connection: object, size: int, *, deadline: float) -> bytes:
    value = bytearray()
    receive = getattr(connection, "recv", None)
    file_descriptor = None
    if not callable(receive):
        fileno = getattr(connection, "fileno", None)
        if not callable(fileno):
            raise OcrWorkerProtocolError("OCR RPC reader is invalid")
        file_descriptor = fileno()
    while len(value) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise TimeoutError("OCR RPC read timed out")
        if callable(receive):
            settimeout = getattr(connection, "settimeout", None)
            if callable(settimeout):
                settimeout(remaining)
            chunk = receive(size - len(value))
        else:
            readable, _, _ = select.select(
                (file_descriptor,),
                (),
                (),
                remaining,
            )
            if not readable:
                raise TimeoutError("OCR RPC read timed out")
            chunk = os.read(file_descriptor, size - len(value))
        if not chunk:
            raise EOFError("OCR RPC peer closed the connection")
        value.extend(chunk)
    return bytes(value)


_REMOTE_ERRORS: dict[OcrFailureCode, type[OcrAdapterError]] = {
    OcrFailureCode.DEPENDENCY_UNAVAILABLE: OcrDependencyUnavailableError,
    OcrFailureCode.EXECUTABLE_UNAVAILABLE: OcrExecutableUnavailableError,
    OcrFailureCode.MODEL_MISSING: OcrModelMissingError,
    OcrFailureCode.MODEL_INTEGRITY: OcrModelIntegrityError,
    OcrFailureCode.LANGUAGE_UNAVAILABLE: OcrLanguageUnavailableError,
    OcrFailureCode.OFFLINE_POLICY: OcrOfflinePolicyError,
    OcrFailureCode.DEVICE_UNAVAILABLE: OcrDeviceUnavailableError,
    OcrFailureCode.RECOGNITION_MISS: OcrRecognitionMissError,
    OcrFailureCode.TIMEOUT: OcrInferenceTimeoutError,
    OcrFailureCode.RESOURCE_EXHAUSTED: OcrResourceExhaustedError,
    OcrFailureCode.ENGINE_ERROR: OcrEngineExecutionError,
    OcrFailureCode.INVALID_OUTPUT: OcrInvalidEngineOutputError,
    OcrFailureCode.OUTPUT_TRUNCATED: OcrOutputTruncatedError,
    OcrFailureCode.WORKER_DIED: OcrWorkerDiedError,
    OcrFailureCode.PROTOCOL_ERROR: OcrWorkerProtocolError,
}


def _remote_error(
    response: dict[str, object],
    logs: str,
) -> OcrAdapterError:
    try:
        code = OcrFailureCode(str(response["failure_code"]))
    except (KeyError, ValueError):
        return OcrWorkerProtocolError(f"external OCR returned an invalid failure code: {logs}")
    error_type = str(response.get("error_type", "remote-error"))[:128]
    message = " ".join(str(response.get("message", "")).split())[:512]
    bounded_logs = " ".join(logs.split())[-512:]
    detail = f"{error_type}: {message}"
    if bounded_logs:
        detail += f"; logs={bounded_logs}"
    return _REMOTE_ERRORS[code](detail)


__all__ = ["ExternalOcrSpec", "ExternalOcrWorker"]
