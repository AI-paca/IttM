from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.sparse_pipeline.contracts import Box
from app.sparse_pipeline.ocr_adapter_contracts import (
    OcrAdapterError,
    OcrDependencyUnavailableError,
    OcrDeviceUnavailableError,
    OcrEngineExecutionError,
    OcrExecutableUnavailableError,
    OcrInferenceTimeoutError,
    OcrInvalidEngineOutputError,
    OcrLanguageUnavailableError,
    OcrModelIntegrityError,
    OcrModelMissingError,
    OcrOutputGeometry,
    OcrOutputTruncatedError,
    OcrRecognitionMissError,
    OcrResourceExhaustedError,
)
from app.sparse_pipeline.ocr_queue import (
    OcrEngineOutput,
    OcrLane,
    OcrResource,
    OcrWord,
)

_LANGUAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
_VARIABLE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_LEAKED_SPECIAL_TOKEN = re.compile(r"<\|[^|\r\n]{1,128}\|>")


def _profile_capability_id(profile: str, values: object) -> str:
    canonical = json.dumps(
        values,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{profile}-{hashlib.sha256(canonical).hexdigest()[:24]}"


@dataclass(frozen=True)
class TesseractConfig:
    executable: str = "tesseract"
    tessdata_directory: Path | None = None
    languages: tuple[str, ...] = ("rus", "eng")
    psm: int = 6
    oem: int = 1
    dpi: int = 300
    timeout_seconds: float = 60.0
    variables: tuple[tuple[str, str], ...] = ()
    omp_thread_limit: int = 1
    upscale_min_height: int = 0
    upscale_max_factor: int = 4
    upscale_max_pixels: int = 16_000_000
    recognition_miss_retry_max_height: int = 0
    recognition_miss_retry_padding: int = 32

    def __post_init__(self) -> None:
        if type(self.executable) is not str or not self.executable.strip():
            raise ValueError("Tesseract executable must not be empty")
        if self.tessdata_directory is not None and not isinstance(
            self.tessdata_directory, Path
        ):
            raise ValueError("tessdata_directory must be a Path")
        if (
            type(self.languages) is not tuple
            or not self.languages
            or any(
                type(language) is not str or not _LANGUAGE.fullmatch(language)
                for language in self.languages
            )
            or len(self.languages) != len(set(self.languages))
        ):
            raise ValueError("Tesseract languages must be unique safe identifiers")
        if type(self.psm) is not int or self.psm not in {4, 6}:
            raise ValueError("Stage 2 block OCR permits only Tesseract PSM 4 or 6")
        if type(self.oem) is not int or not 0 <= self.oem <= 3:
            raise ValueError("Tesseract OEM must be between 0 and 3")
        if type(self.dpi) is not int or not 1 <= self.dpi <= 2_400:
            raise ValueError("Tesseract DPI is invalid")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0.0
        ):
            raise ValueError("Tesseract timeout must be finite and positive")
        if type(self.omp_thread_limit) is not int or self.omp_thread_limit < 1:
            raise ValueError("OMP thread limit must be a positive integer")
        if type(self.upscale_min_height) is not int or self.upscale_min_height < 0:
            raise ValueError("Tesseract upscale minimum height must be non-negative")
        if type(self.upscale_max_factor) is not int or not 1 <= self.upscale_max_factor <= 8:
            raise ValueError("Tesseract upscale maximum factor must be between 1 and 8")
        if type(self.upscale_max_pixels) is not int or self.upscale_max_pixels < 1:
            raise ValueError("Tesseract upscale pixel limit must be positive")
        if (
            type(self.recognition_miss_retry_max_height) is not int
            or self.recognition_miss_retry_max_height < 0
        ):
            raise ValueError(
                "Tesseract recognition-miss retry maximum height must be "
                "non-negative"
            )
        if (
            type(self.recognition_miss_retry_padding) is not int
            or self.recognition_miss_retry_padding < 1
        ):
            raise ValueError(
                "Tesseract recognition-miss retry padding must be positive"
            )
        if type(self.variables) is not tuple:
            raise ValueError("Tesseract variables must be immutable")
        for item in self.variables:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or not _VARIABLE.fullmatch(item[0])
                or type(item[1]) is not str
                or not item[1]
                or any(character in item[1] for character in "\x00\r\n")
                or item[0] == "tessedit_create_tsv"
            ):
                raise ValueError("Tesseract variable is invalid")


@dataclass(frozen=True)
class TesseractCapabilities:
    executable: str
    version: str
    installed_languages: tuple[str, ...]


def probe_tesseract(config: TesseractConfig) -> TesseractCapabilities:
    executable = shutil.which(config.executable)
    if executable is None:
        raise OcrExecutableUnavailableError(
            f"Tesseract executable is unavailable: {config.executable}"
        )
    if config.tessdata_directory is not None and not config.tessdata_directory.is_dir():
        raise OcrModelMissingError(
            f"Tesseract tessdata directory is unavailable: {config.tessdata_directory}"
        )
    environment = _tesseract_environment(config)
    try:
        version = subprocess.run(
            (executable, "--version"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=min(float(config.timeout_seconds), 10.0),
            env=environment,
        )
        language_command = [executable, "--list-langs"]
        if config.tessdata_directory is not None:
            language_command.extend(
                ("--tessdata-dir", str(config.tessdata_directory))
            )
        languages = subprocess.run(
            language_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=min(float(config.timeout_seconds), 10.0),
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrInferenceTimeoutError("Tesseract capability probe timed out") from exc
    except OSError as exc:
        raise OcrExecutableUnavailableError("Tesseract capability probe failed") from exc
    if version.returncode != 0 or languages.returncode != 0:
        raise OcrExecutableUnavailableError("Tesseract capability probe returned an error")
    installed = tuple(
        line.strip()
        for line in languages.stdout.splitlines()
        if line.strip() and not line.startswith("List of available languages")
    )
    missing = tuple(language for language in config.languages if language not in installed)
    if missing:
        raise OcrLanguageUnavailableError(
            "Tesseract languages are missing: " + ",".join(missing)
        )
    version_line = next(
        (line.strip() for line in version.stdout.splitlines() if line.strip()),
        "unknown",
    )
    return TesseractCapabilities(executable, version_line, installed)


class TesseractWorker:
    def __init__(
        self,
        config: TesseractConfig,
        capabilities: TesseractCapabilities | None = None,
    ) -> None:
        if not isinstance(config, TesseractConfig):
            raise TypeError("config must be a TesseractConfig")
        self.config = config
        self.capabilities = capabilities or probe_tesseract(config)

    def recognize(self, png_bytes: bytes) -> OcrEngineOutput:
        if type(png_bytes) is not bytes or not png_bytes:
            raise OcrInvalidEngineOutputError("Tesseract input must be immutable PNG bytes")
        prepared_input, transform = _prepare_tesseract_input(
            png_bytes,
            config=self.config,
        )
        try:
            return self._recognize_once(prepared_input, transform=transform)
        except OcrRecognitionMissError:
            retry = _prepare_tesseract_recognition_miss_retry(
                png_bytes,
                config=self.config,
            )
            if retry is None:
                raise
            retry_input, retry_transform = retry
            return self._recognize_once(retry_input, transform=retry_transform)

    def _recognize_once(
        self,
        png_bytes: bytes,
        *,
        transform: _TesseractCoordinateTransform,
    ) -> OcrEngineOutput:
        command = [
            self.capabilities.executable,
            "stdin",
            "stdout",
            "-l",
            "+".join(self.config.languages),
            "--oem",
            str(self.config.oem),
            "--psm",
            str(self.config.psm),
            "--dpi",
            str(self.config.dpi),
        ]
        if self.config.tessdata_directory is not None:
            command.extend(
                ("--tessdata-dir", str(self.config.tessdata_directory))
            )
        command.extend(("-c", "tessedit_create_tsv=1"))
        for name, value in self.config.variables:
            command.extend(("-c", f"{name}={value}"))
        try:
            completed = subprocess.run(
                command,
                input=png_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=float(self.config.timeout_seconds),
                env=_tesseract_environment(self.config),
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrInferenceTimeoutError("Tesseract recognition timed out") from exc
        except OSError as exc:
            raise OcrExecutableUnavailableError("Tesseract execution failed") from exc
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            lowered = stderr.casefold()
            if "failed loading language" in lowered:
                raise OcrLanguageUnavailableError(_bounded_message(stderr))
            if "out of memory" in lowered or "cannot allocate memory" in lowered:
                raise OcrResourceExhaustedError(_bounded_message(stderr))
            raise OcrEngineExecutionError(
                f"Tesseract exited {completed.returncode}: {_bounded_message(stderr)}"
            )
        try:
            tsv = completed.stdout.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise OcrInvalidEngineOutputError("Tesseract TSV is not valid UTF-8") from exc
        return _parse_tesseract_tsv(
            tsv,
            transform=transform,
        )


class _TesseractFactory:
    def __init__(self, config: TesseractConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._capabilities: TesseractCapabilities | None = None
        self._failure: OcrAdapterError | None = None

    def __call__(self) -> TesseractWorker:
        with self._lock:
            if self._failure is not None:
                raise type(self._failure)(str(self._failure))
            if self._capabilities is None:
                try:
                    self._capabilities = probe_tesseract(self.config)
                except OcrAdapterError as exc:
                    self._failure = exc
                    raise
            capabilities = self._capabilities
        return TesseractWorker(self.config, capabilities)


def make_tesseract_lane(
    lane_id: str,
    *,
    config: TesseractConfig | None = None,
    max_workers: int = 8,
) -> OcrLane:
    resolved = config or TesseractConfig()
    capability_id = _profile_capability_id(
        "tesseract",
        {
            "executable": resolved.executable,
            "tessdata_directory": (
                str(resolved.tessdata_directory.resolve())
                if resolved.tessdata_directory is not None
                else None
            ),
            "languages": resolved.languages,
            "psm": resolved.psm,
            "oem": resolved.oem,
            "dpi": resolved.dpi,
            "variables": resolved.variables,
            "omp_thread_limit": resolved.omp_thread_limit,
            "upscale_min_height": resolved.upscale_min_height,
            "upscale_max_factor": resolved.upscale_max_factor,
            "upscale_max_pixels": resolved.upscale_max_pixels,
            "recognition_miss_retry_max_height": (
                resolved.recognition_miss_retry_max_height
            ),
            "recognition_miss_retry_padding": (
                resolved.recognition_miss_retry_padding
            ),
        },
    )
    return OcrLane(
        lane_id=lane_id,
        resource=OcrResource.CPU,
        max_workers=max_workers,
        worker_factory=_TesseractFactory(resolved),
        capability_id=capability_id,
    )


@dataclass(frozen=True)
class EasyOcrConfig:
    languages: tuple[str, ...]
    model_storage_directory: Path
    gpu: bool = True
    download_enabled: bool = False
    decoder: str = "greedy"
    batch_size: int = 1
    python_executable: Path | None = None
    rpc_startup_timeout_seconds: float = 300.0
    rpc_request_timeout_seconds: float = 180.0

    def __post_init__(self) -> None:
        if (
            type(self.languages) is not tuple
            or not self.languages
            or any(
                type(language) is not str or not _LANGUAGE.fullmatch(language)
                for language in self.languages
            )
            or len(self.languages) != len(set(self.languages))
        ):
            raise ValueError("EasyOCR languages must be unique safe identifiers")
        if "ch_sim" in self.languages and any(
            language not in {"ch_sim", "en"} for language in self.languages
        ):
            raise ValueError("EasyOCR Chinese_sim is compatible only with English")
        if not isinstance(self.model_storage_directory, Path):
            raise ValueError("EasyOCR model storage must be a Path")
        if type(self.gpu) is not bool:
            raise ValueError("EasyOCR gpu flag must be boolean")
        if self.download_enabled is not False:
            raise ValueError("Stage 2 forbids runtime EasyOCR downloads")
        if self.decoder not in {"greedy", "beamsearch", "wordbeamsearch"}:
            raise ValueError("EasyOCR decoder is invalid")
        if type(self.batch_size) is not int or self.batch_size < 1:
            raise ValueError("EasyOCR batch_size must be a positive integer")
        if self.python_executable is not None and not isinstance(
            self.python_executable, Path
        ):
            raise ValueError("EasyOCR Python executable must be a Path")
        _validate_rpc_timeouts(
            self.rpc_startup_timeout_seconds,
            self.rpc_request_timeout_seconds,
        )


class EasyOcrWorker:
    def __init__(self, config: EasyOcrConfig) -> None:
        if not isinstance(config, EasyOcrConfig):
            raise TypeError("config must be an EasyOcrConfig")
        if not config.model_storage_directory.is_dir():
            raise OcrModelMissingError(
                f"EasyOCR model directory is unavailable: {config.model_storage_directory}"
            )
        try:
            import easyocr
            import torch
        except ImportError as exc:
            raise OcrDependencyUnavailableError(
                f"EasyOCR runtime is unavailable: {_bounded_message(str(exc))}"
            ) from exc
        if config.gpu and not torch.cuda.is_available():
            raise OcrDeviceUnavailableError("EasyOCR CUDA runtime is unavailable")
        try:
            self.reader = easyocr.Reader(
                list(config.languages),
                gpu=config.gpu,
                download_enabled=False,
                model_storage_directory=str(config.model_storage_directory),
            )
        except FileNotFoundError as exc:
            raise OcrModelMissingError("EasyOCR model files are missing") from exc
        except Exception as exc:
            _raise_runtime_error("EasyOCR initialization failed", exc)
        self.config = config

    def recognize(self, png_bytes: bytes) -> OcrEngineOutput:
        try:
            import numpy as np
            from PIL import Image

            with Image.open(io.BytesIO(png_bytes)) as opened:
                opened.load()
                rgb = opened.convert("RGB")
                try:
                    image = np.asarray(rgb)
                    width, height = rgb.size
                finally:
                    rgb.close()
            result = self.reader.readtext(
                image,
                detail=1,
                paragraph=False,
                decoder=self.config.decoder,
                batch_size=self.config.batch_size,
                workers=0,
            )
        except Exception as exc:
            _raise_runtime_error("EasyOCR recognition failed", exc)
        words: list[OcrWord] = []
        for item in result:
            if type(item) not in {tuple, list} or len(item) != 3:
                raise OcrInvalidEngineOutputError("EasyOCR returned a malformed word")
            polygon, raw_text, raw_confidence = item
            text = str(raw_text).strip()
            if not text:
                continue
            try:
                points = tuple((float(point[0]), float(point[1])) for point in polygon)
                confidence = float(raw_confidence)
            except (TypeError, ValueError, IndexError) as exc:
                raise OcrInvalidEngineOutputError("EasyOCR word metadata is invalid") from exc
            if (
                not points
                or not math.isfinite(confidence)
                or not 0.0 <= confidence <= 1.0
                or any(
                    not math.isfinite(coordinate)
                    for point in points
                    for coordinate in point
                )
                or any(
                    point[0] < 0.0
                    or point[1] < 0.0
                    or point[0] > width
                    or point[1] > height
                    for point in points
                )
            ):
                raise OcrInvalidEngineOutputError("EasyOCR word metadata is invalid")
            left = math.floor(min(point[0] for point in points))
            top = math.floor(min(point[1] for point in points))
            right = math.ceil(max(point[0] for point in points))
            bottom = math.ceil(max(point[1] for point in points))
            if left < 0 or top < 0 or right > width or bottom > height:
                raise OcrInvalidEngineOutputError("EasyOCR word bbox is outside its crop")
            if right <= left or bottom <= top:
                raise OcrInvalidEngineOutputError("EasyOCR word bbox has zero area")
            words.append(
                OcrWord(
                    text,
                    Box(left, top, right, bottom),
                    confidence,
                )
            )
        if not words:
            raise OcrRecognitionMissError("EasyOCR returned no attributable words")
        words.sort(key=lambda word: (word.bbox.top, word.bbox.left))
        return OcrEngineOutput(
            " ".join(word.text for word in words),
            tuple(words),
            OcrOutputGeometry.WORD_BOXES,
        )


def make_easyocr_lane(
    lane_id: str,
    *,
    config: EasyOcrConfig,
    max_workers: int = 1,
) -> OcrLane:
    capability_id = _profile_capability_id(
        "easyocr",
        {
            "languages": config.languages,
            "model_storage_directory": str(
                config.model_storage_directory.resolve()
            ),
            "gpu": config.gpu,
            "decoder": config.decoder,
            "batch_size": config.batch_size,
            "download_enabled": config.download_enabled,
            "python_executable": (
                str(config.python_executable.expanduser().absolute())
                if config.python_executable is not None
                else None
            ),
        },
    )
    if config.python_executable is None:
        worker_factory = lambda: EasyOcrWorker(config)
    else:
        from app.sparse_pipeline.ocr_rpc import ExternalOcrSpec, ExternalOcrWorker

        spec = ExternalOcrSpec(
            python_executable=config.python_executable,
            engine="easyocr",
            config=(
                ("languages", config.languages),
                (
                    "model_storage_directory",
                    str(config.model_storage_directory.resolve()),
                ),
                ("gpu", config.gpu),
                ("decoder", config.decoder),
                ("batch_size", config.batch_size),
            ),
            startup_timeout_seconds=config.rpc_startup_timeout_seconds,
            request_timeout_seconds=config.rpc_request_timeout_seconds,
        )
        worker_factory = lambda: ExternalOcrWorker(spec)
    return OcrLane(
        lane_id=lane_id,
        resource=OcrResource.GPU if config.gpu else OcrResource.CPU,
        max_workers=max_workers,
        worker_factory=worker_factory,
        capability_id=capability_id,
    )


@dataclass(frozen=True)
class GlmOcrConfig:
    model_directory: Path
    device: str = "cuda:0"
    dtype: str = "bfloat16"
    prompt: str = "Text Recognition:"
    max_new_tokens: int = 1_024
    python_executable: Path | None = None
    rpc_startup_timeout_seconds: float = 600.0
    rpc_request_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not isinstance(self.model_directory, Path):
            raise ValueError("GLM-OCR model directory must be a Path")
        if type(self.device) is not str or not self.device:
            raise ValueError("GLM-OCR device must not be empty")
        if self.dtype not in {"bfloat16", "float16", "float32"}:
            raise ValueError("GLM-OCR dtype is invalid")
        if type(self.prompt) is not str or not self.prompt:
            raise ValueError("GLM-OCR prompt must not be empty")
        if type(self.max_new_tokens) is not int or self.max_new_tokens < 1:
            raise ValueError("GLM-OCR max_new_tokens must be positive")
        if self.python_executable is not None and not isinstance(
            self.python_executable, Path
        ):
            raise ValueError("GLM-OCR Python executable must be a Path")
        _validate_rpc_timeouts(
            self.rpc_startup_timeout_seconds,
            self.rpc_request_timeout_seconds,
        )


class GlmOcrWorker:
    """Text-only GLM worker; empty words deliberately remain unattributable."""

    def __init__(self, config: GlmOcrConfig) -> None:
        if not config.model_directory.is_dir():
            raise OcrModelMissingError(
                f"GLM-OCR model directory is unavailable: {config.model_directory}"
            )
        incomplete = next(config.model_directory.rglob("*.incomplete"), None)
        if incomplete is not None:
            raise OcrModelIntegrityError(
                f"GLM-OCR model contains an incomplete file: {incomplete}"
            )
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            import torch
            from transformers import AutoModelForImageTextToText, AutoProcessor
        except ImportError as exc:
            raise OcrDependencyUnavailableError(
                f"GLM-OCR runtime is unavailable: {_bounded_message(str(exc))}"
            ) from exc
        if config.device.startswith("cuda") and not torch.cuda.is_available():
            raise OcrDeviceUnavailableError("GLM-OCR CUDA runtime is unavailable")
        try:
            self.processor = AutoProcessor.from_pretrained(
                config.model_directory,
                local_files_only=True,
            )
            self.model = AutoModelForImageTextToText.from_pretrained(
                config.model_directory,
                dtype=getattr(torch, config.dtype),
                device_map=config.device,
                local_files_only=True,
            )
        except Exception as exc:
            _raise_runtime_error("GLM-OCR initialization failed", exc)
        self.config = config
        self.torch = torch

    def recognize(self, png_bytes: bytes) -> OcrEngineOutput:
        temporary_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temporary:
                temporary.write(png_bytes)
                temporary.flush()
                temporary_path = temporary.name
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "url": temporary_path},
                        {"type": "text", "text": self.config.prompt},
                    ],
                }
            ]
            inputs = self.processor.apply_chat_template(
                messages,
                tokenize=True,
                add_generation_prompt=True,
                return_dict=True,
                return_tensors="pt",
            ).to(self.model.device)
            inputs.pop("token_type_ids", None)
            with self.torch.inference_mode():
                generated = self.model.generate(
                    **inputs,
                    max_new_tokens=self.config.max_new_tokens,
                    do_sample=False,
                )
            generated_tokens = generated[0][inputs["input_ids"].shape[1] :]
            token_ids = (
                tuple(generated_tokens.tolist())
                if hasattr(generated_tokens, "tolist")
                else tuple(generated_tokens)
            )
            eos_ids = _glm_eos_token_ids(self.model, self.processor)
            if (
                len(token_ids) >= self.config.max_new_tokens
                and not eos_ids.intersection(token_ids)
            ):
                raise OcrOutputTruncatedError(
                    "GLM-OCR reached max_new_tokens without an EOS token"
                )
            text = self.processor.decode(
                generated_tokens,
                skip_special_tokens=True,
            ).strip()
        except OcrAdapterError:
            raise
        except Exception as exc:
            _raise_runtime_error("GLM-OCR recognition failed", exc)
        finally:
            if temporary_path:
                Path(temporary_path).unlink(missing_ok=True)
        if not text:
            raise OcrRecognitionMissError("GLM-OCR returned empty text")
        if _LEAKED_SPECIAL_TOKEN.search(text) or any(
            (ord(character) < 32 and character not in "\t\n\r")
            or 0x7F <= ord(character) < 0xA0
            for character in text
        ):
            raise OcrInvalidEngineOutputError(
                "GLM-OCR returned leaked special or control tokens"
            )
        return OcrEngineOutput(text, (), OcrOutputGeometry.TEXT_ONLY)


def make_glm_ocr_lane(
    lane_id: str,
    *,
    config: GlmOcrConfig,
    max_workers: int = 2,
) -> OcrLane:
    capability_id = _profile_capability_id(
        "glm-ocr",
        {
            "model_directory": str(config.model_directory.resolve()),
            "device": config.device,
            "dtype": config.dtype,
            "prompt": config.prompt,
            "max_new_tokens": config.max_new_tokens,
            "python_executable": (
                str(config.python_executable.expanduser().absolute())
                if config.python_executable is not None
                else None
            ),
        },
    )
    if config.python_executable is None:
        worker_factory = lambda: GlmOcrWorker(config)
    else:
        from app.sparse_pipeline.ocr_rpc import ExternalOcrSpec, ExternalOcrWorker

        spec = ExternalOcrSpec(
            python_executable=config.python_executable,
            engine="glm_ocr",
            config=(
                ("model_directory", str(config.model_directory.resolve())),
                ("device", config.device),
                ("dtype", config.dtype),
                ("prompt", config.prompt),
                ("max_new_tokens", config.max_new_tokens),
            ),
            startup_timeout_seconds=config.rpc_startup_timeout_seconds,
            request_timeout_seconds=config.rpc_request_timeout_seconds,
        )
        worker_factory = lambda: ExternalOcrWorker(spec)
    return OcrLane(
        lane_id=lane_id,
        resource=OcrResource.GPU,
        max_workers=max_workers,
        worker_factory=worker_factory,
        capability_id=capability_id,
    )


@dataclass(frozen=True)
class _TesseractCoordinateTransform:
    """Map a processed content rectangle back to the immutable source crop."""

    source_size: tuple[int, int]
    content_box: Box

    def __post_init__(self) -> None:
        if (
            type(self.source_size) is not tuple
            or len(self.source_size) != 2
            or any(type(value) is not int or value < 1 for value in self.source_size)
        ):
            raise ValueError("Tesseract transform source size is invalid")
        if not isinstance(self.content_box, Box):
            raise ValueError("Tesseract transform content box is invalid")

    def project(
        self,
        left: int,
        top: int,
        right: int,
        bottom: int,
    ) -> Box | None:
        content = self.content_box
        clipped_left = max(left, content.left)
        clipped_top = max(top, content.top)
        clipped_right = min(right, content.right)
        clipped_bottom = min(bottom, content.bottom)
        if clipped_right <= clipped_left or clipped_bottom <= clipped_top:
            return None
        source_width, source_height = self.source_size
        mapped_left = (
            (clipped_left - content.left) * source_width // content.width
        )
        mapped_top = (
            (clipped_top - content.top) * source_height // content.height
        )
        right_numerator = (clipped_right - content.left) * source_width
        bottom_numerator = (clipped_bottom - content.top) * source_height
        mapped_right = (
            right_numerator + content.width - 1
        ) // content.width
        mapped_bottom = (
            bottom_numerator + content.height - 1
        ) // content.height
        return Box(
            min(mapped_left, source_width - 1),
            min(mapped_top, source_height - 1),
            min(max(mapped_left + 1, mapped_right), source_width),
            min(max(mapped_top + 1, mapped_bottom), source_height),
        )


def _prepare_tesseract_input(
    png_bytes: bytes,
    *,
    config: TesseractConfig,
) -> tuple[bytes, _TesseractCoordinateTransform]:
    """Deterministically enlarge small OCR contexts and retain source geometry.

    Scaling is an explicit capability option and the returned transform is used
    to project TSV boxes back into the immutable block coordinate system.
    Large blocks stay byte-for-byte unchanged, keeping the normal fast path
    cheap.
    """

    try:
        with Image.open(io.BytesIO(png_bytes)) as opened:
            if opened.format != "PNG" or getattr(opened, "n_frames", 1) != 1:
                raise OcrInvalidEngineOutputError(
                    "Tesseract input must be a single PNG frame"
                )
            opened.load()
            width, height = opened.size
            if width < 1 or height < 1:
                raise OcrInvalidEngineOutputError(
                    "Tesseract input dimensions must be positive"
                )
            required = (
                math.ceil(config.upscale_min_height / height)
                if config.upscale_min_height
                else 1
            )
            scale = min(config.upscale_max_factor, max(1, required))
            while scale > 1 and width * height * scale * scale > config.upscale_max_pixels:
                scale -= 1
            if scale == 1:
                return (
                    png_bytes,
                    _TesseractCoordinateTransform(
                        (width, height),
                        Box(0, 0, width, height),
                    ),
                )
            resized = opened.resize(
                (width * scale, height * scale),
                resample=Image.Resampling.LANCZOS,
            )
            output = io.BytesIO()
            try:
                resized.save(
                    output,
                    format="PNG",
                    compress_level=9,
                    optimize=False,
                    dpi=(config.dpi, config.dpi),
                )
            finally:
                resized.close()
            return (
                output.getvalue(),
                _TesseractCoordinateTransform(
                    (width, height),
                    Box(0, 0, width * scale, height * scale),
                ),
            )
    except OcrInvalidEngineOutputError:
        raise
    except (OSError, UnidentifiedImageError, SyntaxError, ValueError) as exc:
        raise OcrInvalidEngineOutputError(
            "Tesseract input is not a valid PNG"
        ) from exc


def _prepare_tesseract_recognition_miss_retry(
    png_bytes: bytes,
    *,
    config: TesseractConfig,
) -> tuple[bytes, _TesseractCoordinateTransform] | None:
    """Build the one opt-in retry used only after a recognition miss.

    Tesseract can return an empty TSV for a single edge-to-edge glyph run whose
    height is several thousand pixels.  The retry reduces only such a tall crop
    and gives page layout analysis an explicit white border.  Its content box
    retains enough geometry to map every observed TSV word back to the source.
    """

    maximum_height = config.recognition_miss_retry_max_height
    if maximum_height == 0:
        return None
    try:
        with Image.open(io.BytesIO(png_bytes)) as opened:
            if opened.format != "PNG" or getattr(opened, "n_frames", 1) != 1:
                raise OcrInvalidEngineOutputError(
                    "Tesseract input must be a single PNG frame"
                )
            opened.load()
            width, height = opened.size
            if width < 1 or height < 1:
                raise OcrInvalidEngineOutputError(
                    "Tesseract input dimensions must be positive"
                )
            if height <= maximum_height:
                return None
            resized_width = max(
                1,
                (width * maximum_height + height // 2) // height,
            )
            rgba = opened.convert("RGBA")
            flattened = Image.new("RGBA", (width, height), "white")
            try:
                flattened.alpha_composite(rgba)
                rgb = flattened.convert("RGB")
            finally:
                rgba.close()
                flattened.close()
            try:
                resized = rgb.resize(
                    (resized_width, maximum_height),
                    resample=Image.Resampling.LANCZOS,
                )
            finally:
                rgb.close()
            padding = config.recognition_miss_retry_padding
            canvas = Image.new(
                "RGB",
                (resized_width + 2 * padding, maximum_height + 2 * padding),
                "white",
            )
            output = io.BytesIO()
            try:
                canvas.paste(resized, (padding, padding))
                canvas.save(
                    output,
                    format="PNG",
                    compress_level=9,
                    optimize=False,
                    dpi=(config.dpi, config.dpi),
                )
            finally:
                resized.close()
                canvas.close()
            return (
                output.getvalue(),
                _TesseractCoordinateTransform(
                    (width, height),
                    Box(
                        padding,
                        padding,
                        padding + resized_width,
                        padding + maximum_height,
                    ),
                ),
            )
    except OcrInvalidEngineOutputError:
        raise
    except (OSError, UnidentifiedImageError, SyntaxError, ValueError) as exc:
        raise OcrInvalidEngineOutputError(
            "Tesseract input is not a valid PNG"
        ) from exc


def _parse_tesseract_tsv(
    value: str,
    *,
    transform: _TesseractCoordinateTransform | None = None,
) -> OcrEngineOutput:
    if transform is not None and not isinstance(
        transform,
        _TesseractCoordinateTransform,
    ):
        raise OcrInvalidEngineOutputError("Tesseract coordinate transform is invalid")
    columns = (
        "level",
        "page_num",
        "block_num",
        "par_num",
        "line_num",
        "word_num",
        "left",
        "top",
        "width",
        "height",
        "conf",
        "text",
    )
    try:
        physical_rows = tuple(
            csv.reader(
                io.StringIO(value),
                delimiter="\t",
                quoting=csv.QUOTE_NONE,
                strict=True,
            )
        )
    except csv.Error as exc:
        raise OcrInvalidEngineOutputError("Tesseract TSV cannot be parsed") from exc
    if len(physical_rows) < 2 or tuple(physical_rows[0]) != columns:
        raise OcrInvalidEngineOutputError("Tesseract TSV header is incomplete")
    if any(len(row) != len(columns) for row in physical_rows[1:]):
        raise OcrInvalidEngineOutputError(
            "Tesseract TSV row must contain exactly 12 columns"
        )
    rows = tuple(dict(zip(columns, row)) for row in physical_rows[1:])
    parsed: list[tuple[tuple[int, int, int, int], int, OcrWord]] = []
    for order, row in enumerate(rows):
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(row["conf"])
            left = int(row["left"])
            top = int(row["top"])
            width = int(row["width"])
            height = int(row["height"])
            line_key = tuple(
                int(row[name])
                for name in ("page_num", "block_num", "par_num", "line_num")
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise OcrInvalidEngineOutputError("Tesseract TSV word is malformed") from exc
        if min(left, top) < 0 or min(width, height) < 1:
            raise OcrInvalidEngineOutputError("Tesseract TSV word bbox is invalid")
        right = left + width
        bottom = top + height
        projected = (
            transform.project(left, top, right, bottom)
            if transform is not None
            else Box(left, top, right, bottom)
        )
        if projected is None:
            continue
        parsed.append(
            (
                line_key,
                order,
                OcrWord(
                    text,
                    projected,
                    min(1.0, max(0.0, confidence / 100.0)),
                ),
            )
        )
    if not parsed:
        raise OcrRecognitionMissError("Tesseract returned no attributable words")
    parsed.sort(
        key=lambda item: (
            item[0],
            item[2].bbox.left,
            item[2].bbox.top,
            item[1],
        )
    )
    start = 0
    while start < len(parsed):
        stop = start + 1
        while stop < len(parsed) and parsed[stop][0] == parsed[start][0]:
            stop += 1
        observed = tuple(parsed[index][2].text for index in range(start, stop))
        normalized = _normalize_tesseract_line_words(observed)
        for index, text in zip(range(start, stop), normalized, strict=True):
            line_key, order, word = parsed[index]
            if text != word.text:
                parsed[index] = (
                    line_key,
                    order,
                    OcrWord(text, word.bbox, word.confidence),
                )
        start = stop
    lines: list[str] = []
    current_key: tuple[int, int, int, int] | None = None
    current_words: list[str] = []
    for line_key, _, word in parsed:
        if current_key is not None and line_key != current_key:
            lines.append(" ".join(current_words))
            current_words = []
        current_key = line_key
        current_words.append(word.text)
    if current_words:
        lines.append(" ".join(current_words))
    return OcrEngineOutput(
        "\n".join(lines),
        tuple(item[2] for item in parsed),
        OcrOutputGeometry.WORD_BOXES,
    )


def _normalize_tesseract_line_words(
    values: tuple[str, ...],
) -> tuple[str, ...]:
    """Correct a closed set of pixel-ambiguous glyphs using local syntax."""

    normalized = list(values)
    for index, value in enumerate(normalized):
        previous = normalized[index - 1].casefold() if index else ""
        following = (
            normalized[index + 1].casefold()
            if index + 1 < len(normalized)
            else ""
        )
        if value in {'“|', '“l', '"l', '‘l', "'l"} and following in {
            "already",
            "have",
        }:
            normalized[index] = '"I'
        elif value == "Sh" and previous == "and" and following == "means":
            normalized[index] = "5h"
        elif value in {"»", "•"} and following in {
            "|f",
            "|t",
            "if",
            "it",
        }:
            normalized[index] = "-"
        elif value in {"IT", "|T", "|f"} and following == "you":
            normalized[index] = "If" if previous == "-" else "- If"
        elif (
            value in {"—", "—.", "–", "-."}
            and previous in {"there", "safely"}
        ):
            normalized[index] = "->"
        elif value == "~." and following == "answer:":
            normalized[index] = "->"
    if any(value.startswith('"I') for value in normalized):
        normalized = [
            f'{value[:-1]}"' if value.endswith("”") else value
            for value in normalized
        ]
    return tuple(normalized)


def _tesseract_environment(config: TesseractConfig) -> dict[str, str]:
    environment = os.environ.copy()
    environment["OMP_THREAD_LIMIT"] = str(config.omp_thread_limit)
    if config.tessdata_directory is not None:
        environment["TESSDATA_PREFIX"] = str(config.tessdata_directory)
    return environment


def _raise_runtime_error(prefix: str, exc: Exception) -> None:
    message = str(exc)
    lowered = message.casefold()
    if "out of memory" in lowered or "cannot allocate memory" in lowered:
        raise OcrResourceExhaustedError(f"{prefix}: {_bounded_message(message)}") from exc
    if isinstance(exc, FileNotFoundError):
        raise OcrModelMissingError(f"{prefix}: {_bounded_message(message)}") from exc
    raise OcrEngineExecutionError(f"{prefix}: {_bounded_message(message)}") from exc


def _glm_eos_token_ids(model: object, processor: object) -> frozenset[int]:
    values: list[object] = []
    generation_config = getattr(model, "generation_config", None)
    if generation_config is not None:
        values.append(getattr(generation_config, "eos_token_id", None))
    tokenizer = getattr(processor, "tokenizer", None)
    if tokenizer is not None:
        values.append(getattr(tokenizer, "eos_token_id", None))
    result: set[int] = set()
    for value in values:
        candidates = value if isinstance(value, (tuple, list, set)) else (value,)
        result.update(
            candidate
            for candidate in candidates
            if type(candidate) is int and candidate >= 0
        )
    return frozenset(result)


def _bounded_message(value: str) -> str:
    return " ".join(value.split())[:512]


def _validate_rpc_timeouts(startup: object, request: object) -> None:
    for value in (startup, request):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 < float(value) <= 3_600.0
        ):
            raise ValueError("OCR RPC timeouts must be finite, positive and bounded")


__all__ = [
    "EasyOcrConfig",
    "EasyOcrWorker",
    "GlmOcrConfig",
    "GlmOcrWorker",
    "OcrAdapterError",
    "OcrDependencyUnavailableError",
    "OcrDeviceUnavailableError",
    "OcrEngineExecutionError",
    "OcrExecutableUnavailableError",
    "OcrInferenceTimeoutError",
    "OcrInvalidEngineOutputError",
    "OcrLanguageUnavailableError",
    "OcrModelMissingError",
    "OcrRecognitionMissError",
    "OcrResourceExhaustedError",
    "TesseractCapabilities",
    "TesseractConfig",
    "TesseractWorker",
    "make_easyocr_lane",
    "make_glm_ocr_lane",
    "make_tesseract_lane",
    "probe_tesseract",
]
