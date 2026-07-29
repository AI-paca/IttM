from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import app.sparse_pipeline.ocr_adapters as adapters
import app.sparse_pipeline.ocr_rpc as rpc
from app.sparse_pipeline.contracts import Box
from app.sparse_pipeline.ocr_adapter_contracts import (
    OcrInferenceTimeoutError,
    OcrModelMissingError,
    OcrOutputGeometry,
    OcrWorkerDiedError,
    OcrWorkerProtocolError,
)
from app.sparse_pipeline.ocr_queue import OcrResource


_HELPER_SOURCE = r"""
import hashlib
import json
import os
import struct
import sys
import time


def read_exact(connection, size):
    value = bytearray()
    while len(value) < size:
        chunk = connection.read(size - len(value))
        if not chunk:
            raise EOFError
        value.extend(chunk)
    return bytes(value)


def receive(connection):
    header_size = struct.unpack(">I", read_exact(connection, 4))[0]
    header = json.loads(read_exact(connection, header_size).decode("utf-8"))
    payload = read_exact(connection, header["payload_length"])
    return header, payload


def send(connection, header, payload=b""):
    value = dict(header)
    value["payload_length"] = len(payload)
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    connection.write(struct.pack(">I", len(encoded)) + encoded + payload)
    connection.flush()


engine = sys.argv[1]
mode = sys.argv[2]
protocol_input = sys.stdin.buffer
protocol_output = sys.stdout.buffer
if mode == "startup-hang":
    time.sleep(30)
    raise SystemExit(9)
send(protocol_output, {"type": "ready", "engine": engine})
while True:
    request, payload = receive(protocol_input)
    if request.get("type") == "close":
        send(protocol_output, {"type": "closed"})
        raise SystemExit(0)
    request_id = request.get("request_id")
    digest = hashlib.sha256(payload).hexdigest()
    if (
        request.get("type") != "recognize"
        or type(request_id) is not int
        or request_id < 1
        or request.get("payload_sha256") != digest
    ):
        raise SystemExit(8)
    kind = payload.decode("ascii")
    common = {"request_id": request_id, "payload_sha256": digest}
    if kind == "typed-error":
        send(
            protocol_output,
            {
                **common,
                "type": "error",
                "failure_code": "model_missing",
                "error_type": "HermeticMissingModel",
                "message": "fixture model absent",
            },
        )
        continue
    if kind == "invalid-error-code":
        send(
            protocol_output,
            {
                **common,
                "type": "error",
                "failure_code": "not-a-real-code",
                "error_type": "Bogus",
                "message": "bogus",
            },
        )
        continue
    if kind == "hang":
        time.sleep(30)
        raise SystemExit(7)
    if kind == "dribble":
        value = {
            **common,
            "type": "result",
            "geometry": "text_only",
            "text": "late",
            "words": [],
            "payload_length": 0,
        }
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        framed = struct.pack(">I", len(encoded)) + encoded
        for byte in framed[:4]:
            protocol_output.write(bytes((byte,)))
            protocol_output.flush()
            time.sleep(0.03)
        protocol_output.write(framed[4:])
        protocol_output.flush()
        continue
    if kind == "logs":
        marker = "END-OF-BOUNDED-LOG"
        sys.stderr.write("L" * (40960 - len(marker)) + marker)
        sys.stderr.flush()
    if kind == "bad-digest":
        common["payload_sha256"] = "0" * 64
    if kind == "bad-id":
        common["request_id"] = True
    if kind == "bad-text-type":
        send(
            protocol_output,
            {
                **common,
                "type": "result",
                "geometry": "text_only",
                "text": 7,
                "words": [],
            },
        )
        continue
    if kind == "bad-bbox-type":
        send(
            protocol_output,
            {
                **common,
                "type": "result",
                "geometry": "word_boxes",
                "text": "Alpha",
                "words": [
                    {
                        "text": "Alpha",
                        "bbox": ["1", "2", "6", "8"],
                        "confidence": "0.75",
                    }
                ],
            },
        )
        continue
    if kind == "words":
        send(
            protocol_output,
            {
                **common,
                "type": "result",
                "geometry": "word_boxes",
                "text": "Alpha",
                "words": [
                    {
                        "text": "Alpha",
                        "bbox": [1, 2, 6, 8],
                        "confidence": 0.75,
                    }
                ],
            },
        )
        continue
    send(
        protocol_output,
        {
            **common,
            "type": "result",
            "geometry": "text_only",
            "text": "中文",
            "words": [],
        },
    )
"""


def _spec(
    *,
    engine: str = "easyocr",
    startup_timeout: float = 2.0,
    request_timeout: float = 2.0,
) -> rpc.ExternalOcrSpec:
    return rpc.ExternalOcrSpec(
        python_executable=Path("/usr/bin/python"),
        engine=engine,
        config=(("fixture", "hermetic"),),
        startup_timeout_seconds=startup_timeout,
        request_timeout_seconds=request_timeout,
    )


@pytest.fixture
def helper_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str], list[tuple[tuple[str, ...], dict[str, Any]]]]:
    helper = tmp_path / "rpc_fixture.py"
    helper.write_text(_HELPER_SOURCE, encoding="utf-8")
    real_popen = subprocess.Popen

    def launch(mode: str = "normal") -> list[tuple[tuple[str, ...], dict[str, Any]]]:
        calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

        def substitute(command: object, **kwargs: Any) -> subprocess.Popen[bytes]:
            original = tuple(str(value) for value in command)  # type: ignore[arg-type]
            calls.append((original, kwargs.copy()))
            replacement = (
                str(Path("/usr/bin/python").resolve()),
                "-u",
                str(helper),
                original[original.index("--engine") + 1],
                mode,
            )
            return real_popen(replacement, **kwargs)

        monkeypatch.setattr(rpc.subprocess, "Popen", substitute)
        return calls

    return launch


def _raw_frame(header: object, payload: bytes = b"") -> bytes:
    encoded = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(encoded)) + encoded + payload


def test_frame_roundtrip_binds_the_exact_binary_payload() -> None:
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=0)
    writer = os.fdopen(write_fd, "wb", buffering=0)
    try:
        payload = b"\x89PNG\r\n\x1a\nfixture"
        rpc._send_frame(
            writer,
            {
                "type": "recognize",
                "request_id": 17,
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "payload_length": 999,
            },
            payload,
        )
        header, observed = rpc._receive_frame(reader, timeout=1.0)
    finally:
        writer.close()
        reader.close()
    assert observed == payload
    assert header["payload_length"] == len(payload)
    assert header["payload_sha256"] == hashlib.sha256(observed).hexdigest()


@pytest.mark.parametrize(
    "wire",
    (
        struct.pack(">I", 1) + b"{",
        _raw_frame([], b""),
        _raw_frame({"type": "x"}, b""),
        _raw_frame({"payload_length": -1}, b""),
        _raw_frame({"payload_length": 128 * 1024 * 1024 + 1}, b""),
    ),
)
def test_frame_decoder_rejects_malformed_or_unbounded_headers(wire: bytes) -> None:
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=0)
    writer = os.fdopen(write_fd, "wb", buffering=0)
    try:
        writer.write(wire)
        writer.flush()
        with pytest.raises(OcrWorkerProtocolError):
            rpc._receive_frame(reader, timeout=0.1)
    finally:
        writer.close()
        reader.close()


def test_real_rpc_worker_child_maps_startup_failure_without_loading_ocr(
    tmp_path: Path,
) -> None:
    missing_models = tmp_path / "definitely-missing"
    spec = rpc.ExternalOcrSpec(
        python_executable=Path("/usr/bin/python"),
        engine="easyocr",
        config=(
            ("languages", ("en", "ru")),
            ("model_storage_directory", str(missing_models)),
            ("gpu", False),
            ("decoder", "greedy"),
            ("batch_size", 1),
        ),
        startup_timeout_seconds=5.0,
        request_timeout_seconds=1.0,
    )
    with pytest.raises(OcrModelMissingError, match="OcrModelMissingError"):
        rpc.ExternalOcrWorker(spec)


def test_external_worker_roundtrips_word_boxes_and_unattributable_text(
    helper_launcher: Callable[[str], object],
) -> None:
    helper_launcher("normal")
    worker = rpc.ExternalOcrWorker(_spec())
    try:
        boxed = worker.recognize(b"words")
        text_only = worker.recognize(b"text")
    finally:
        worker.close()
    assert boxed.geometry is OcrOutputGeometry.WORD_BOXES
    assert boxed.text == "Alpha"
    assert boxed.words[0].bbox == Box(1, 2, 6, 8)
    assert boxed.words[0].confidence == 0.75
    assert text_only.geometry is OcrOutputGeometry.TEXT_ONLY
    assert (text_only.text, text_only.words) == ("中文", ())


def test_remote_typed_error_is_non_poisoning_and_keeps_request_sequence(
    helper_launcher: Callable[[str], object],
) -> None:
    helper_launcher("normal")
    worker = rpc.ExternalOcrWorker(_spec())
    try:
        with pytest.raises(OcrModelMissingError, match="fixture model absent"):
            worker.recognize(b"typed-error")
        assert worker._process.poll() is None
        assert worker.recognize(b"words").text == "Alpha"
    finally:
        worker.close()


@pytest.mark.parametrize(
    "payload",
    (b"bad-id", b"bad-digest", b"bad-text-type", b"bad-bbox-type"),
)
def test_malformed_response_identity_digest_or_schema_fails_closed(
    payload: bytes,
    helper_launcher: Callable[[str], object],
) -> None:
    helper_launcher("normal")
    worker = rpc.ExternalOcrWorker(_spec())
    with pytest.raises(OcrWorkerProtocolError):
        worker.recognize(payload)
    assert worker._closed is True
    assert worker._process.poll() is not None


def test_invalid_remote_failure_code_is_a_poisoning_protocol_error(
    helper_launcher: Callable[[str], object],
) -> None:
    helper_launcher("normal")
    worker = rpc.ExternalOcrWorker(_spec())
    with pytest.raises(OcrWorkerProtocolError, match="invalid failure code"):
        worker.recognize(b"invalid-error-code")
    assert worker._closed is True
    assert worker._process.poll() is not None


def test_request_timeout_terminates_the_stuck_child(
    helper_launcher: Callable[[str], object],
) -> None:
    helper_launcher("normal")
    worker = rpc.ExternalOcrWorker(_spec(request_timeout=0.05))
    with pytest.raises(OcrInferenceTimeoutError, match="request timed out"):
        worker.recognize(b"hang")
    assert worker._closed is True
    assert worker._process.poll() is not None


def test_request_timeout_is_one_absolute_deadline_not_a_per_byte_budget(
    helper_launcher: Callable[[str], object],
) -> None:
    helper_launcher("normal")
    worker = rpc.ExternalOcrWorker(_spec(request_timeout=0.05))
    started = time.monotonic()
    with pytest.raises(OcrInferenceTimeoutError, match="request timed out"):
        worker.recognize(b"dribble")
    assert time.monotonic() - started < 0.2
    assert worker._closed is True
    assert worker._process.poll() is not None


def test_startup_timeout_terminates_the_stuck_child(
    helper_launcher: Callable[[str], object],
) -> None:
    helper_launcher("startup-hang")
    with pytest.raises(OcrInferenceTimeoutError, match="startup timed out"):
        rpc.ExternalOcrWorker(_spec(startup_timeout=0.05))


def test_close_is_idempotent_and_makes_future_calls_fail(
    helper_launcher: Callable[[str], object],
) -> None:
    helper_launcher("normal")
    worker = rpc.ExternalOcrWorker(_spec())
    worker.close()
    worker.close()
    assert worker._closed is True
    assert worker._process.poll() is not None
    with pytest.raises(OcrWorkerDiedError, match="closed"):
        worker.recognize(b"words")


def test_child_logs_are_tail_bounded(
    helper_launcher: Callable[[str], object],
) -> None:
    helper_launcher("normal")
    worker = rpc.ExternalOcrWorker(_spec())
    try:
        assert worker.recognize(b"logs").text == "中文"
        deadline = time.monotonic() + 1.0
        while "END-OF-BOUNDED-LOG" not in worker.bounded_logs:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert len(worker.bounded_logs.encode("utf-8")) <= 32 * 1024
        assert worker.bounded_logs.endswith("END-OF-BOUNDED-LOG")
    finally:
        worker.close()


def test_spawn_uses_exact_interpreter_immutable_config_and_offline_environment(
    helper_launcher: Callable[
        [str], list[tuple[tuple[str, ...], dict[str, Any]]]
    ],
) -> None:
    calls = helper_launcher("normal")
    spec = _spec(engine="glm_ocr")
    worker = rpc.ExternalOcrWorker(spec)
    worker.close()
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[0] == str(spec.python_executable)
    assert command[1:4] == (
        "-u",
        "-m",
        "app.sparse_pipeline.ocr_rpc_worker",
    )
    assert command[command.index("--engine") + 1] == "glm_ocr"
    assert json.loads(command[command.index("--config-json") + 1]) == {
        "fixture": "hermetic"
    }
    environment = kwargs["env"]
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"
    assert environment["PYTHONPATH"].split(os.pathsep)[0] == str(
        Path(rpc.__file__).resolve().parents[2]
    )
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.PIPE
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert "pass_fds" not in kwargs
    assert "--fd" not in command


def test_spawn_preserves_an_absolute_venv_python_symlink(
    tmp_path: Path,
    helper_launcher: Callable[
        [str], list[tuple[tuple[str, ...], dict[str, Any]]]
    ],
) -> None:
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(Path("/usr/bin/python"))
    calls = helper_launcher("normal")
    spec = rpc.ExternalOcrSpec(
        python_executable=venv_python,
        engine="easyocr",
        config=(("fixture", "hermetic"),),
        startup_timeout_seconds=2.0,
        request_timeout_seconds=2.0,
    )
    worker = rpc.ExternalOcrWorker(spec)
    worker.close()
    assert calls[0][0][0] == str(venv_python)
    assert calls[0][0][0] != str(venv_python.resolve())


def test_adapter_external_hooks_freeze_exact_easy_and_glm_specs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_directory = tmp_path / "models"
    model_directory.mkdir()
    observed: list[rpc.ExternalOcrSpec] = []

    class FakeExternalWorker:
        def __init__(self, spec: rpc.ExternalOcrSpec) -> None:
            observed.append(spec)

    monkeypatch.setattr(rpc, "ExternalOcrWorker", FakeExternalWorker)
    easy_config = adapters.EasyOcrConfig(
        ("en", "ru"),
        model_directory,
        gpu=True,
        decoder="beamsearch",
        batch_size=3,
        python_executable=Path("/usr/bin/python"),
        rpc_startup_timeout_seconds=11.0,
        rpc_request_timeout_seconds=12.0,
    )
    glm_config = adapters.GlmOcrConfig(
        model_directory,
        device="cuda:1",
        dtype="float16",
        prompt="Text Recognition:",
        max_new_tokens=321,
        python_executable=Path("/usr/bin/python"),
        rpc_startup_timeout_seconds=21.0,
        rpc_request_timeout_seconds=22.0,
    )
    easy_lane = adapters.make_easyocr_lane("easy-external", config=easy_config)
    glm_lane = adapters.make_glm_ocr_lane("glm-external", config=glm_config)
    assert easy_lane.resource is OcrResource.GPU
    assert glm_lane.resource is OcrResource.GPU
    easy_lane.worker_factory()
    glm_lane.worker_factory()
    assert len(observed) == 2
    easy_spec, glm_spec = observed
    assert easy_spec.python_executable == Path("/usr/bin/python")
    assert easy_spec.engine == "easyocr"
    assert dict(easy_spec.config) == {
        "languages": ("en", "ru"),
        "model_storage_directory": str(model_directory.resolve()),
        "gpu": True,
        "decoder": "beamsearch",
        "batch_size": 3,
    }
    assert (
        easy_spec.startup_timeout_seconds,
        easy_spec.request_timeout_seconds,
    ) == (11.0, 12.0)
    assert glm_spec.python_executable == Path("/usr/bin/python")
    assert glm_spec.engine == "glm_ocr"
    assert dict(glm_spec.config) == {
        "model_directory": str(model_directory.resolve()),
        "device": "cuda:1",
        "dtype": "float16",
        "prompt": "Text Recognition:",
        "max_new_tokens": 321,
    }
    assert (
        glm_spec.startup_timeout_seconds,
        glm_spec.request_timeout_seconds,
    ) == (21.0, 22.0)


def test_external_adapter_capability_keeps_distinct_venv_symlink_identity(
    tmp_path: Path,
) -> None:
    model_directory = tmp_path / "models"
    model_directory.mkdir()
    first_python = tmp_path / "first-venv" / "bin" / "python"
    second_python = tmp_path / "second-venv" / "bin" / "python"
    for executable in (first_python, second_python):
        executable.parent.mkdir(parents=True)
        executable.symlink_to(Path("/usr/bin/python"))
    first = adapters.make_easyocr_lane(
        "easy-first-venv",
        config=adapters.EasyOcrConfig(
            ("en", "ru"),
            model_directory,
            python_executable=first_python,
        ),
    )
    second = adapters.make_easyocr_lane(
        "easy-second-venv",
        config=adapters.EasyOcrConfig(
            ("en", "ru"),
            model_directory,
            python_executable=second_python,
        ),
    )
    assert first_python.resolve() == second_python.resolve()
    assert first.capability_id != second.capability_id
