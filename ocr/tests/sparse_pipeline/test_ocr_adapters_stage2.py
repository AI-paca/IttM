from __future__ import annotations

import builtins
import contextlib
import io
import json
import math
import os
import socket
import subprocess
import sys
import types
from pathlib import Path
from typing import Callable

import pytest
from PIL import Image

import app.sparse_pipeline.ocr_adapters as adapters
from app.sparse_pipeline.contracts import Box
from app.sparse_pipeline.ocr_adapter_contracts import (
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
from app.sparse_pipeline.ocr_queue import OcrEngineOutput, OcrResource, OcrWord


@pytest.fixture(autouse=True)
def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("adapter contract tests forbid network access")

    monkeypatch.setattr(socket, "create_connection", forbidden)


def _png_bytes(*, width: int = 12, height: int = 8) -> bytes:
    output = io.BytesIO()
    image = Image.new("RGB", (width, height), "white")
    try:
        image.save(output, format="PNG")
    finally:
        image.close()
    return output.getvalue()


def _tsv() -> str:
    return (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
        "1\t1\t0\t0\t0\t0\t0\t0\t12\t8\t-1\t\n"
        "5\t1\t1\t1\t1\t1\t3\t1\t4\t2\t95\tAlpha\n"
        "5\t1\t1\t1\t1\t2\t8\t1\t3\t2\t80\t17\n"
        "5\t1\t1\t1\t2\t1\t1\t5\t5\t2\t75\t中文\n"
    )


def _empty_tsv(*, width: int = 12, height: int = 8) -> str:
    return (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
        f"1\t1\t0\t0\t0\t0\t0\t0\t{width}\t{height}\t-1\t\n"
    )


def _completed(
    args: object,
    *,
    stdout: str | bytes,
    stderr: str | bytes = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[object]:
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_importing_adapter_modules_loads_no_real_ocr_gpu_or_network_runtime() -> None:
    repository = Path(__file__).resolve().parents[3]
    code = """
import json
import sys
sys.path.insert(0, 'ocr')
import app.sparse_pipeline.ocr_adapter_contracts
import app.sparse_pipeline.ocr_adapters
banned_roots = {
    'easyocr', 'onnxruntime', 'paddle', 'paddleocr', 'pytesseract',
    'tensorflow', 'tesserocr', 'torch', 'transformers',
}
banned = sorted(
    name for name in sys.modules
    if name.split('.', 1)[0] in banned_roots
    or name.startswith('app.engines')
)
print(json.dumps(banned))
raise SystemExit(bool(banned))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(completed.stdout) == []


def test_typed_adapter_errors_have_stable_failure_codes() -> None:
    expected = {
        OcrDependencyUnavailableError: OcrFailureCode.DEPENDENCY_UNAVAILABLE,
        OcrExecutableUnavailableError: OcrFailureCode.EXECUTABLE_UNAVAILABLE,
        OcrModelMissingError: OcrFailureCode.MODEL_MISSING,
        OcrModelIntegrityError: OcrFailureCode.MODEL_INTEGRITY,
        OcrLanguageUnavailableError: OcrFailureCode.LANGUAGE_UNAVAILABLE,
        OcrOfflinePolicyError: OcrFailureCode.OFFLINE_POLICY,
        OcrDeviceUnavailableError: OcrFailureCode.DEVICE_UNAVAILABLE,
        OcrRecognitionMissError: OcrFailureCode.RECOGNITION_MISS,
        OcrInferenceTimeoutError: OcrFailureCode.TIMEOUT,
        OcrResourceExhaustedError: OcrFailureCode.RESOURCE_EXHAUSTED,
        OcrEngineExecutionError: OcrFailureCode.ENGINE_ERROR,
        OcrInvalidEngineOutputError: OcrFailureCode.INVALID_OUTPUT,
        OcrOutputTruncatedError: OcrFailureCode.OUTPUT_TRUNCATED,
        OcrWorkerDiedError: OcrFailureCode.WORKER_DIED,
        OcrWorkerProtocolError: OcrFailureCode.PROTOCOL_ERROR,
    }
    assert {error: error("x").code for error in expected} == expected
    assert OcrInferenceTimeoutError.retryable is True
    assert OcrInferenceTimeoutError.worker_poisoned is True
    assert OcrResourceExhaustedError.worker_poisoned is True
    assert OcrWorkerDiedError.retryable is True
    assert OcrWorkerProtocolError.worker_poisoned is True


def test_tesseract_profile_freezes_exact_language_order_and_safe_flags() -> None:
    config = adapters.TesseractConfig()
    assert config.languages == ("rus", "eng")
    assert (config.psm, config.oem, config.omp_thread_limit) == (6, 1, 1)
    assert config.upscale_min_height == 0
    assert config.recognition_miss_retry_max_height == 0
    assert config.recognition_miss_retry_padding == 32
    with pytest.raises(ValueError, match="PSM"):
        adapters.TesseractConfig(psm=13)
    with pytest.raises(ValueError, match="languages"):
        adapters.TesseractConfig(languages=("eng", "chi_sim", "eng"))
    with pytest.raises(ValueError, match="languages"):
        adapters.TesseractConfig(languages=("eng", "bad\nlang"))
    with pytest.raises(ValueError, match="variable"):
        adapters.TesseractConfig(variables=(("tessedit_create_tsv", "0"),))
    with pytest.raises(ValueError, match="minimum height"):
        adapters.TesseractConfig(upscale_min_height=-1)
    with pytest.raises(ValueError, match="maximum factor"):
        adapters.TesseractConfig(upscale_max_factor=9)
    with pytest.raises(ValueError, match="retry maximum height"):
        adapters.TesseractConfig(recognition_miss_retry_max_height=-1)
    with pytest.raises(ValueError, match="retry padding"):
        adapters.TesseractConfig(recognition_miss_retry_padding=0)


def test_tesseract_preflight_uses_exact_tessdata_and_never_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    monkeypatch.setattr(adapters.shutil, "which", lambda _name: "/fake/tesseract")

    def fake_run(command: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
        values = tuple(command)  # type: ignore[arg-type]
        calls.append((values, kwargs))
        if "--version" in values:
            return _completed(command, stdout="tesseract 5.5.2\n")
        return _completed(
            command,
            stdout="List of available languages (3):\nrus\neng\nchi_sim\n",
        )

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    config = adapters.TesseractConfig(tessdata_directory=tessdata)
    capability = adapters.probe_tesseract(config)

    assert capability.executable == "/fake/tesseract"
    assert capability.version == "tesseract 5.5.2"
    assert capability.installed_languages == ("rus", "eng", "chi_sim")
    assert calls[1][0] == (
        "/fake/tesseract",
        "--list-langs",
        "--tessdata-dir",
        str(tessdata),
    )
    assert all(call[1]["env"]["OMP_THREAD_LIMIT"] == "1" for call in calls)

    calls.clear()

    def missing_language(
        command: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[object]:
        values = tuple(command)  # type: ignore[arg-type]
        if "--version" in values:
            return _completed(command, stdout="tesseract 5.5.2\n")
        return _completed(
            command,
            stdout="List of available languages (2):\neng\nrus\n",
        )

    monkeypatch.setattr(adapters.subprocess, "run", missing_language)
    with pytest.raises(OcrLanguageUnavailableError, match="chi_sim"):
        adapters.probe_tesseract(config)
    assert config.languages == ("eng", "chi_sim", "rus")


def test_tesseract_tsv_command_and_crop_local_words_are_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[object]:
        calls.append((command, kwargs))
        return _completed(command, stdout=_tsv().encode(), stderr=b"")

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    config = adapters.TesseractConfig(
        tessdata_directory=tessdata,
        variables=(("preserve_interword_spaces", "1"),),
    )
    capability = adapters.TesseractCapabilities(
        "/fake/tesseract",
        "tesseract 5.5.2",
        ("eng", "chi_sim", "rus"),
    )
    output = adapters.TesseractWorker(config, capability).recognize(_png_bytes())

    assert calls[0][0] == [
        "/fake/tesseract",
        "stdin",
        "stdout",
        "-l",
        "eng+chi_sim+rus",
        "--oem",
        "1",
        "--psm",
        "6",
        "--dpi",
        "300",
        "--tessdata-dir",
        str(tessdata),
        "-c",
        "tessedit_create_tsv=1",
        "-c",
        "preserve_interword_spaces=1",
    ]
    assert "tsv" not in calls[0][0]
    assert calls[0][1]["input"] == _png_bytes()
    assert calls[0][1]["env"]["OMP_THREAD_LIMIT"] == "1"
    assert output.geometry is OcrOutputGeometry.WORD_BOXES
    assert output.text == "Alpha 17\n中文"
    assert tuple(word.text for word in output.words) == ("Alpha", "17", "中文")
    assert tuple(word.bbox for word in output.words) == (
        Box(3, 1, 7, 3),
        Box(8, 1, 11, 3),
        Box(1, 5, 6, 7),
    )
    assert tuple(word.confidence for word in output.words) == (0.95, 0.8, 0.75)


def test_tesseract_tsv_ascii_quote_is_a_literal_word_and_cannot_join_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quote_tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t1\t1\t1\t2\t99\t\"\n"
        "5\t1\t1\t1\t1\t2\t3\t1\t4\t2\t98\tAlpha\n"
        "5\t1\t1\t1\t2\t1\t1\t5\t4\t2\t97\tBeta\n"
    )

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[object]:
        return _completed(command, stdout=quote_tsv.encode(), stderr=b"")

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    config = adapters.TesseractConfig()
    capability = adapters.TesseractCapabilities(
        "/fake/tesseract",
        "tesseract 5.5.2",
        config.languages,
    )

    output = adapters.TesseractWorker(config, capability).recognize(_png_bytes())

    assert output.text == '\" Alpha\nBeta'
    assert tuple(word.text for word in output.words) == ('\"', "Alpha", "Beta")
    assert all("\n" not in word.text and "\t" not in word.text for word in output.words)


@pytest.mark.parametrize(
    "word_row",
    (
        "5\t1\t1\t1\t1\t1\t1\t1\t1\t2\t99\n",
        "5\t1\t1\t1\t1\t1\t1\t1\t1\t2\t99\tAlpha\textra\n",
    ),
    ids=("missing-column", "extra-column"),
)
def test_tesseract_tsv_malformed_column_count_fails_closed(word_row: str) -> None:
    malformed = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
        + word_row
    )

    with pytest.raises(OcrInvalidEngineOutputError, match="exactly 12 columns"):
        adapters._parse_tesseract_tsv(malformed)


def test_tesseract_runtime_language_failure_is_typed_and_has_no_retry_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
        nonlocal calls
        calls += 1
        return _completed(
            command,
            stdout=b"",
            stderr=b"Failed loading language 'chi_sim'",
            returncode=1,
        )

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    config = adapters.TesseractConfig(
        recognition_miss_retry_max_height=4,
        recognition_miss_retry_padding=2,
    )
    capability = adapters.TesseractCapabilities(
        "/fake/tesseract",
        "tesseract 5.5.2",
        config.languages,
    )
    with pytest.raises(OcrLanguageUnavailableError):
        adapters.TesseractWorker(config, capability).recognize(_png_bytes())
    assert calls == 1
    assert config.languages == ("eng", "chi_sim", "rus")


def test_tesseract_runtime_engine_failure_has_no_recognition_miss_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
        nonlocal calls
        calls += 1
        return _completed(
            command,
            stdout=b"",
            stderr=b"internal engine failure",
            returncode=1,
        )

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    config = adapters.TesseractConfig(
        recognition_miss_retry_max_height=4,
        recognition_miss_retry_padding=2,
    )
    capability = adapters.TesseractCapabilities(
        "/fake/tesseract",
        "tesseract 5.5.2",
        config.languages,
    )
    with pytest.raises(OcrEngineExecutionError):
        adapters.TesseractWorker(config, capability).recognize(_png_bytes())
    assert calls == 1


def test_tesseract_recognition_miss_retry_downscales_pads_and_maps_boxes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_buffer = io.BytesIO()
    source_image = Image.new("RGB", (120, 80), "black")
    try:
        source_image.save(source_buffer, format="PNG")
    finally:
        source_image.close()
    source = source_buffer.getvalue()
    observed: list[bytes] = []

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[object]:
        payload = kwargs["input"]
        assert isinstance(payload, bytes)
        observed.append(payload)
        if len(observed) == 1:
            return _completed(
                command,
                stdout=_empty_tsv(width=120, height=80).encode(),
                stderr=b"",
            )
        retry_tsv = (
            _empty_tsv(width=36, height=26)
            + "5\t1\t1\t1\t1\t1\t8\t5\t15\t10\t99\tSAMPLE\n"
        )
        return _completed(command, stdout=retry_tsv.encode(), stderr=b"")

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    config = adapters.TesseractConfig(
        recognition_miss_retry_max_height=20,
        recognition_miss_retry_padding=3,
    )
    capability = adapters.TesseractCapabilities(
        "/fake/tesseract",
        "tesseract 5.5.2",
        config.languages,
    )

    output = adapters.TesseractWorker(config, capability).recognize(source)

    assert len(observed) == 2
    assert observed[0] == source
    with Image.open(io.BytesIO(observed[1])) as retry_image:
        retry_image.load()
        assert retry_image.size == (36, 26)
        assert retry_image.getpixel((0, 0)) == (255, 255, 255)
        assert retry_image.getpixel((3, 3)) == (0, 0, 0)
    assert output.text == "SAMPLE"
    assert output.words == (OcrWord("SAMPLE", Box(20, 8, 80, 48), 0.99),)


def test_tesseract_recognition_miss_default_is_off_and_enabled_retry_runs_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def empty(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[object]:
        nonlocal calls
        calls += 1
        return _completed(command, stdout=_empty_tsv().encode(), stderr=b"")

    monkeypatch.setattr(adapters.subprocess, "run", empty)
    default_config = adapters.TesseractConfig()
    default_capability = adapters.TesseractCapabilities(
        "/fake/tesseract",
        "tesseract 5.5.2",
        default_config.languages,
    )
    with pytest.raises(OcrRecognitionMissError):
        adapters.TesseractWorker(default_config, default_capability).recognize(
            _png_bytes()
        )
    assert calls == 1

    enabled_config = adapters.TesseractConfig(
        recognition_miss_retry_max_height=4,
        recognition_miss_retry_padding=2,
    )
    enabled_capability = adapters.TesseractCapabilities(
        "/fake/tesseract",
        "tesseract 5.5.2",
        enabled_config.languages,
    )
    with pytest.raises(OcrRecognitionMissError):
        adapters.TesseractWorker(enabled_config, enabled_capability).recognize(
            _png_bytes()
        )
    assert calls == 3


def test_tesseract_small_context_upscale_is_bounded_and_boxes_map_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    observed_size: tuple[int, int] | None = None

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[object]:
        nonlocal observed_size
        payload = kwargs["input"]
        assert isinstance(payload, bytes)
        with Image.open(io.BytesIO(payload)) as opened:
            observed_size = opened.size
        scaled_tsv = _tsv().replace(
            "\t3\t1\t4\t2\t95\tAlpha",
            "\t12\t4\t16\t8\t95\tAlpha",
        ).replace(
            "\t8\t1\t3\t2\t80\t17",
            "\t32\t4\t12\t8\t80\t17",
        ).replace(
            "\t1\t5\t5\t2\t75\t中文",
            "\t4\t20\t20\t8\t75\t中文",
        )
        return _completed(command, stdout=scaled_tsv.encode(), stderr=b"")

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    config = adapters.TesseractConfig(
        tessdata_directory=tessdata,
        upscale_min_height=32,
        upscale_max_factor=4,
        upscale_max_pixels=2_000,
    )
    capability = adapters.TesseractCapabilities(
        "/fake/tesseract",
        "tesseract 5.5.2",
        config.languages,
    )

    output = adapters.TesseractWorker(config, capability).recognize(_png_bytes())

    assert observed_size == (48, 32)
    assert tuple(word.bbox for word in output.words) == (
        Box(3, 1, 7, 3),
        Box(8, 1, 11, 3),
        Box(1, 5, 6, 7),
    )


def test_tesseract_timeout_is_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(command: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(command, 1.0)

    monkeypatch.setattr(adapters.subprocess, "run", timeout)
    config = adapters.TesseractConfig(timeout_seconds=1.0)
    capability = adapters.TesseractCapabilities(
        "/fake/tesseract",
        "tesseract 5.5.2",
        config.languages,
    )
    with pytest.raises(OcrInferenceTimeoutError) as raised:
        adapters.TesseractWorker(config, capability).recognize(_png_bytes())
    assert raised.value.code is OcrFailureCode.TIMEOUT


class _FakeEasyReader:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[object, dict[str, object]]] = []

    def readtext(self, image: object, **kwargs: object) -> object:
        self.calls.append((image, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _install_easy_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: object,
    cuda: bool = True,
) -> tuple[_FakeEasyReader, list[tuple[list[str], dict[str, object]]]]:
    reader = _FakeEasyReader(result)
    constructor_calls: list[tuple[list[str], dict[str, object]]] = []
    easyocr = types.ModuleType("easyocr")

    def make_reader(languages: list[str], **kwargs: object) -> _FakeEasyReader:
        constructor_calls.append((languages, kwargs))
        return reader

    easyocr.Reader = make_reader  # type: ignore[attr-defined]
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: cuda)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "easyocr", easyocr)
    monkeypatch.setitem(sys.modules, "torch", torch)
    return reader, constructor_calls


def test_easyocr_profiles_are_split_offline_and_preserve_observed_boxes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_dir = tmp_path / "easy-models"
    model_dir.mkdir()
    result = [
        ([[1.2, 2.1], [8.1, 2.0], [8.0, 5.8], [1.2, 5.9]], "word", 0.875),
    ]
    reader, constructors = _install_easy_runtime(monkeypatch, result=result)

    en_ru = adapters.EasyOcrConfig(("en", "ru"), model_dir)
    worker = adapters.EasyOcrWorker(en_ru)
    output = worker.recognize(_png_bytes())

    assert constructors == [
        (
            ["en", "ru"],
            {
                "gpu": True,
                "download_enabled": False,
                "model_storage_directory": str(model_dir),
            },
        )
    ]
    assert reader.calls[0][1] == {
        "detail": 1,
        "paragraph": False,
        "decoder": "greedy",
        "batch_size": 1,
        "workers": 0,
    }
    assert output == OcrEngineOutput(
        "word",
        (OcrWord("word", Box(1, 2, 9, 6), 0.875),),
        OcrOutputGeometry.WORD_BOXES,
    )
    assert adapters.EasyOcrConfig(("ch_sim", "en"), model_dir).languages == (
        "ch_sim",
        "en",
    )
    with pytest.raises(ValueError, match="Chinese_sim|compatible"):
        adapters.EasyOcrConfig(("en", "ru", "ch_sim"), model_dir)
    with pytest.raises(ValueError, match="forbids|download"):
        adapters.EasyOcrConfig(("en", "ru"), model_dir, download_enabled=True)


@pytest.mark.parametrize("confidence", (-0.01, 1.01, math.nan, math.inf))
def test_easyocr_rejects_confidence_outside_closed_unit_interval_instead_of_clamping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    confidence: float,
) -> None:
    model_dir = tmp_path / "easy-models"
    model_dir.mkdir()
    polygon = [[1.0, 1.0], [5.0, 1.0], [5.0, 4.0], [1.0, 4.0]]
    _install_easy_runtime(
        monkeypatch,
        result=[(polygon, "observed", confidence)],
    )
    worker = adapters.EasyOcrWorker(
        adapters.EasyOcrConfig(("en", "ru"), model_dir)
    )

    with pytest.raises(OcrInvalidEngineOutputError, match="metadata|confidence"):
        worker.recognize(_png_bytes())


@pytest.mark.parametrize(
    "polygon",
    (
        [[-0.1, 1.0], [4.0, 1.0], [4.0, 3.0], [0.0, 3.0]],
        [[1.0, 1.0], [12.1, 1.0], [12.0, 3.0], [1.0, 3.0]],
        [[math.nan, 1.0], [4.0, 1.0], [4.0, 3.0], [1.0, 3.0]],
        [[2.0, 2.0], [2.0, 2.0], [2.0, 2.0], [2.0, 2.0]],
    ),
)
def test_easyocr_rejects_unobserved_or_invalid_bbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    polygon: list[list[float]],
) -> None:
    model_dir = tmp_path / "easy-models"
    model_dir.mkdir()
    _install_easy_runtime(monkeypatch, result=[(polygon, "word", 0.9)])
    worker = adapters.EasyOcrWorker(
        adapters.EasyOcrConfig(("en", "ru"), model_dir)
    )
    with pytest.raises(OcrInvalidEngineOutputError, match="metadata|bbox"):
        worker.recognize(_png_bytes())


def test_easyocr_model_dependency_device_miss_and_oom_are_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(OcrModelMissingError):
        adapters.EasyOcrWorker(adapters.EasyOcrConfig(("en", "ru"), missing))

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    _install_easy_runtime(monkeypatch, result=[], cuda=False)
    with pytest.raises(OcrDeviceUnavailableError):
        adapters.EasyOcrWorker(adapters.EasyOcrConfig(("en", "ru"), model_dir))

    _install_easy_runtime(
        monkeypatch,
        result=RuntimeError("CUDA out of memory while allocating tensor"),
    )
    worker = adapters.EasyOcrWorker(
        adapters.EasyOcrConfig(("en", "ru"), model_dir)
    )
    with pytest.raises(OcrResourceExhaustedError) as raised:
        worker.recognize(_png_bytes())
    assert raised.value.worker_poisoned is True


def test_easyocr_dependency_error_is_raised_without_importing_real_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    original_import = builtins.__import__

    def controlled_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name in {"easyocr", "torch"}:
            raise ImportError("hermetic missing dependency")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "easyocr", raising=False)
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(builtins, "__import__", controlled_import)
    with pytest.raises(OcrDependencyUnavailableError):
        adapters.EasyOcrWorker(adapters.EasyOcrConfig(("en", "ru"), model_dir))


class _FakeInputs(dict[str, object]):
    def __init__(self) -> None:
        super().__init__(
            input_ids=types.SimpleNamespace(shape=(1, 2)),
            token_type_ids=object(),
        )
        self.target_device: str | None = None

    def to(self, device: str) -> _FakeInputs:
        self.target_device = device
        return self


class _FakeGlmProcessor:
    def __init__(self, decoded: str) -> None:
        self.decoded = decoded
        self.messages: list[object] = []
        self.temporary_path: str | None = None
        self.decode_calls: list[tuple[object, bool]] = []

    def apply_chat_template(self, messages: object, **kwargs: object) -> _FakeInputs:
        assert kwargs == {
            "tokenize": True,
            "add_generation_prompt": True,
            "return_dict": True,
            "return_tensors": "pt",
        }
        self.messages.append(messages)
        path = messages[0]["content"][0]["url"]  # type: ignore[index]
        self.temporary_path = path
        assert Path(path).is_file()
        return _FakeInputs()

    def decode(self, tokens: object, *, skip_special_tokens: bool) -> str:
        self.decode_calls.append((tokens, skip_special_tokens))
        return self.decoded


class _FakeGlmModel:
    device = "cuda:0"

    def __init__(self, generated: object) -> None:
        self.generated = generated
        self.calls: list[dict[str, object]] = []

    def generate(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if isinstance(self.generated, Exception):
            raise self.generated
        return self.generated


def _install_glm_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    decoded: str,
    generated: object = ((10, 11, 20, 21),),
    cuda: bool = True,
) -> tuple[
    _FakeGlmProcessor,
    _FakeGlmModel,
    list[tuple[Path, dict[str, object]]],
    list[tuple[Path, dict[str, object]]],
]:
    processor = _FakeGlmProcessor(decoded)
    model = _FakeGlmModel(generated)
    processor_loads: list[tuple[Path, dict[str, object]]] = []
    model_loads: list[tuple[Path, dict[str, object]]] = []
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: cuda)  # type: ignore[attr-defined]
    torch.bfloat16 = object()  # type: ignore[attr-defined]
    torch.float16 = object()  # type: ignore[attr-defined]
    torch.float32 = object()  # type: ignore[attr-defined]
    torch.inference_mode = contextlib.nullcontext  # type: ignore[attr-defined]
    transformers = types.ModuleType("transformers")

    class AutoProcessor:
        @staticmethod
        def from_pretrained(path: Path, **kwargs: object) -> _FakeGlmProcessor:
            processor_loads.append((path, kwargs))
            return processor

    class AutoModelForImageTextToText:
        @staticmethod
        def from_pretrained(path: Path, **kwargs: object) -> _FakeGlmModel:
            model_loads.append((path, kwargs))
            return model

    transformers.AutoProcessor = AutoProcessor  # type: ignore[attr-defined]
    transformers.AutoModelForImageTextToText = AutoModelForImageTextToText  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    return processor, model, processor_loads, model_loads


def test_glm_is_strictly_local_text_only_and_never_fabricates_boxes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_dir = tmp_path / "glm"
    model_dir.mkdir()
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    processor, model, processor_loads, model_loads = _install_glm_runtime(
        monkeypatch,
        decoded="第一行\nВторая line",
    )
    config = adapters.GlmOcrConfig(model_dir, max_new_tokens=128)
    worker = adapters.GlmOcrWorker(config)
    output = worker.recognize(_png_bytes())

    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert processor_loads == [(model_dir, {"local_files_only": True})]
    assert model_loads == [
        (
            model_dir,
            {
                "dtype": sys.modules["torch"].bfloat16,
                "device_map": "cuda:0",
                "local_files_only": True,
            },
        )
    ]
    assert output == OcrEngineOutput(
        "第一行\nВторая line",
        (),
        OcrOutputGeometry.TEXT_ONLY,
    )
    assert model.calls[0]["max_new_tokens"] == 128
    assert model.calls[0]["do_sample"] is False
    assert processor.decode_calls[0][1] is True
    assert processor.temporary_path is not None
    assert not Path(processor.temporary_path).exists()
    with pytest.raises(ValueError, match="text-only|word"):
        OcrEngineOutput(
            "forged",
            (OcrWord("forged", Box(0, 0, 1, 1), 1.0),),
            OcrOutputGeometry.TEXT_ONLY,
        )


def test_glm_max_new_tokens_without_eos_is_typed_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_dir = tmp_path / "glm"
    model_dir.mkdir()
    _processor, model, _processor_loads, _model_loads = _install_glm_runtime(
        monkeypatch,
        decoded="truncated but nonempty",
        generated=((10, 11, 20, 21, 22),),
    )
    model.generation_config = types.SimpleNamespace(eos_token_id=99)
    worker = adapters.GlmOcrWorker(
        adapters.GlmOcrConfig(model_dir, max_new_tokens=3)
    )

    with pytest.raises(OcrOutputTruncatedError) as raised:
        worker.recognize(_png_bytes())
    assert raised.value.code is OcrFailureCode.OUTPUT_TRUNCATED


def test_glm_eos_at_token_limit_is_not_reported_as_truncated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_dir = tmp_path / "glm"
    model_dir.mkdir()
    _processor, model, _processor_loads, _model_loads = _install_glm_runtime(
        monkeypatch,
        decoded="complete",
        generated=((10, 11, 20, 21, 99),),
    )
    model.generation_config = types.SimpleNamespace(eos_token_id=(98, 99))
    worker = adapters.GlmOcrWorker(
        adapters.GlmOcrConfig(model_dir, max_new_tokens=3)
    )

    assert worker.recognize(_png_bytes()).text == "complete"


@pytest.mark.parametrize(
    "decoded",
    (
        "observed <|user|> leaked",
        "observed\x00control",
        "observed\x85control",
    ),
)
def test_glm_rejects_leaked_special_and_control_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decoded: str,
) -> None:
    model_dir = tmp_path / "glm"
    model_dir.mkdir()
    _install_glm_runtime(monkeypatch, decoded=decoded)
    worker = adapters.GlmOcrWorker(adapters.GlmOcrConfig(model_dir))

    with pytest.raises(OcrInvalidEngineOutputError) as raised:
        worker.recognize(_png_bytes())
    assert raised.value.code is OcrFailureCode.INVALID_OUTPUT


def test_glm_incomplete_model_is_integrity_failure_before_runtime_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_dir = tmp_path / "glm"
    incomplete = model_dir / "blobs" / "weights.safetensors.incomplete"
    incomplete.parent.mkdir(parents=True)
    incomplete.write_bytes(b"")
    original_import = builtins.__import__
    attempted_runtime_import = False

    def controlled_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        nonlocal attempted_runtime_import
        if name in {"torch", "transformers"}:
            attempted_runtime_import = True
            raise AssertionError("integrity preflight must precede runtime import")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.delitem(sys.modules, "transformers", raising=False)
    monkeypatch.setattr(builtins, "__import__", controlled_import)

    with pytest.raises(OcrModelIntegrityError) as raised:
        adapters.GlmOcrWorker(adapters.GlmOcrConfig(model_dir))
    assert raised.value.code is OcrFailureCode.MODEL_INTEGRITY
    assert attempted_runtime_import is False


def test_glm_empty_text_model_missing_device_and_oom_are_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(OcrModelMissingError):
        adapters.GlmOcrWorker(adapters.GlmOcrConfig(missing))

    model_dir = tmp_path / "glm"
    model_dir.mkdir()
    _install_glm_runtime(monkeypatch, decoded="", cuda=False)
    with pytest.raises(OcrDeviceUnavailableError):
        adapters.GlmOcrWorker(adapters.GlmOcrConfig(model_dir))

    _install_glm_runtime(monkeypatch, decoded="")
    empty_worker = adapters.GlmOcrWorker(adapters.GlmOcrConfig(model_dir))
    with pytest.raises(OcrRecognitionMissError):
        empty_worker.recognize(_png_bytes())

    _install_glm_runtime(
        monkeypatch,
        decoded="unused",
        generated=RuntimeError("CUDA out of memory"),
    )
    oom_worker = adapters.GlmOcrWorker(adapters.GlmOcrConfig(model_dir))
    with pytest.raises(OcrResourceExhaustedError) as raised:
        oom_worker.recognize(_png_bytes())
    assert raised.value.code is OcrFailureCode.RESOURCE_EXHAUSTED


def test_glm_dependency_error_is_raised_without_importing_real_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_dir = tmp_path / "glm"
    model_dir.mkdir()
    original_import = builtins.__import__

    def controlled_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name in {"torch", "transformers"}:
            raise ImportError("hermetic missing dependency")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.delitem(sys.modules, "transformers", raising=False)
    monkeypatch.setattr(builtins, "__import__", controlled_import)
    with pytest.raises(OcrDependencyUnavailableError):
        adapters.GlmOcrWorker(adapters.GlmOcrConfig(model_dir))


def test_lane_factories_keep_resources_and_split_profiles_explicit(
    tmp_path: Path,
) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    tess = adapters.make_tesseract_lane("tess", max_workers=8)
    easy_ru = adapters.make_easyocr_lane(
        "easy-ru",
        config=adapters.EasyOcrConfig(("en", "ru"), model_dir),
    )
    easy_zh = adapters.make_easyocr_lane(
        "easy-zh",
        config=adapters.EasyOcrConfig(("ch_sim", "en"), model_dir),
    )
    glm = adapters.make_glm_ocr_lane(
        "glm",
        config=adapters.GlmOcrConfig(model_dir),
        max_workers=2,
    )
    assert (tess.resource, tess.max_workers) == (OcrResource.CPU, 8)
    assert (easy_ru.resource, easy_ru.max_workers) == (OcrResource.GPU, 1)
    assert (easy_zh.resource, easy_zh.max_workers) == (OcrResource.GPU, 1)
    assert (glm.resource, glm.max_workers) == (OcrResource.GPU, 2)
    assert easy_ru.worker_factory is not easy_zh.worker_factory


def test_capability_id_depends_only_on_exact_adapter_configuration_not_lane_id(
    tmp_path: Path,
) -> None:
    tessdata = tmp_path / "tessdata"
    easy_models = tmp_path / "easy"
    glm_model = tmp_path / "glm"
    for directory in (tessdata, easy_models, glm_model):
        directory.mkdir()

    tess_config = adapters.TesseractConfig(tessdata_directory=tessdata)
    tess_a = adapters.make_tesseract_lane("tess-a", config=tess_config)
    tess_b = adapters.make_tesseract_lane(
        "tess-b",
        config=tess_config,
        max_workers=1,
    )
    tess_changed = adapters.make_tesseract_lane(
        "tess-changed",
        config=adapters.TesseractConfig(
            tessdata_directory=tessdata,
            psm=4,
        ),
    )

    easy_config = adapters.EasyOcrConfig(("en", "ru"), easy_models)
    easy_a = adapters.make_easyocr_lane("easy-a", config=easy_config)
    easy_b = adapters.make_easyocr_lane("easy-b", config=easy_config)
    easy_changed = adapters.make_easyocr_lane(
        "easy-changed",
        config=adapters.EasyOcrConfig(
            ("en", "ru"),
            easy_models,
            decoder="beamsearch",
        ),
    )

    glm_config = adapters.GlmOcrConfig(glm_model, max_new_tokens=128)
    glm_a = adapters.make_glm_ocr_lane("glm-a", config=glm_config)
    glm_b = adapters.make_glm_ocr_lane(
        "glm-b",
        config=glm_config,
        max_workers=1,
    )
    glm_changed = adapters.make_glm_ocr_lane(
        "glm-changed",
        config=adapters.GlmOcrConfig(glm_model, max_new_tokens=256),
    )

    assert tess_a.capability_id == tess_b.capability_id
    assert easy_a.capability_id == easy_b.capability_id
    assert glm_a.capability_id == glm_b.capability_id
    assert tess_changed.capability_id != tess_a.capability_id
    assert easy_changed.capability_id != easy_a.capability_id
    assert glm_changed.capability_id != glm_a.capability_id
    assert len(
        {
            tess_a.capability_id,
            easy_a.capability_id,
            glm_a.capability_id,
        }
    ) == 3


def test_tesseract_retry_configuration_is_part_of_capability_provenance() -> None:
    disabled = adapters.make_tesseract_lane(
        "tess-disabled",
        config=adapters.TesseractConfig(),
    )
    enabled = adapters.make_tesseract_lane(
        "tess-enabled",
        config=adapters.TesseractConfig(
            recognition_miss_retry_max_height=768,
            recognition_miss_retry_padding=32,
        ),
    )
    changed_padding = adapters.make_tesseract_lane(
        "tess-padding",
        config=adapters.TesseractConfig(
            recognition_miss_retry_max_height=768,
            recognition_miss_retry_padding=48,
        ),
    )

    assert len(
        {
            disabled.capability_id,
            enabled.capability_id,
            changed_padding.capability_id,
        }
    ) == 3
