from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class OcrResource(str, Enum):
    CPU = "cpu"
    GPU = "gpu"


class OcrBackend(str, Enum):
    TESSERACT_TSV = "tesseract_tsv"
    EASYOCR = "easyocr"
    GLM_OCR = "glm_ocr"
    TEST_DOUBLE = "test_double"


class OcrOutputGeometry(str, Enum):
    WORD_BOXES = "word_boxes"
    TEXT_ONLY = "text_only"


class OcrAttributionStatus(str, Enum):
    ATTRIBUTED = "attributed"
    UNATTRIBUTABLE = "unattributable"


class OcrFailureCode(str, Enum):
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    EXECUTABLE_UNAVAILABLE = "executable_unavailable"
    MODEL_MISSING = "model_missing"
    MODEL_INTEGRITY = "model_integrity"
    LANGUAGE_UNAVAILABLE = "language_unavailable"
    OFFLINE_POLICY = "offline_policy"
    DEVICE_UNAVAILABLE = "device_unavailable"
    RECOGNITION_MISS = "recognition_miss"
    TIMEOUT = "timeout"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    ENGINE_ERROR = "engine_error"
    INVALID_OUTPUT = "invalid_output"
    OUTPUT_TRUNCATED = "output_truncated"
    WORKER_DIED = "worker_died"
    PROTOCOL_ERROR = "protocol_error"


@dataclass(frozen=True)
class OcrModelFile:
    role: str
    path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if type(self.role) is not str or not self.role:
            raise ValueError("OCR model role must not be empty")
        if type(self.path) is not str or not self.path:
            raise ValueError("OCR model path must not be empty")
        if type(self.size) is not int or self.size < 1:
            raise ValueError("OCR model size must be positive")
        if (
            type(self.sha256) is not str
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ValueError("OCR model digest must be lowercase SHA-256")


@dataclass(frozen=True)
class OcrCapability:
    capability_id: str
    profile_id: str
    backend: OcrBackend
    resource: OcrResource
    output_geometry: OcrOutputGeometry
    engine_version: str
    python_executable: str | None
    executable: str | None
    device: str
    languages: tuple[str, ...]
    models: tuple[OcrModelFile, ...]
    options: tuple[tuple[str, str], ...]
    offline_enforced: bool

    def __post_init__(self) -> None:
        for name, value in (
            ("capability_id", self.capability_id),
            ("profile_id", self.profile_id),
            ("engine_version", self.engine_version),
            ("device", self.device),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"OCR capability {name} must not be empty")
        if not isinstance(self.backend, OcrBackend):
            raise ValueError("OCR capability backend is invalid")
        if not isinstance(self.resource, OcrResource):
            raise ValueError("OCR capability resource is invalid")
        if not isinstance(self.output_geometry, OcrOutputGeometry):
            raise ValueError("OCR capability output geometry is invalid")
        if self.python_executable is not None and (
            type(self.python_executable) is not str or not self.python_executable
        ):
            raise ValueError("OCR capability Python executable is invalid")
        if self.executable is not None and (type(self.executable) is not str or not self.executable):
            raise ValueError("OCR capability executable is invalid")
        if (
            type(self.languages) is not tuple
            or any(type(item) is not str or not item for item in self.languages)
            or len(self.languages) != len(set(self.languages))
        ):
            raise ValueError("OCR capability languages must be unique and immutable")
        if type(self.models) is not tuple or any(not isinstance(item, OcrModelFile) for item in self.models):
            raise ValueError("OCR capability models must be immutable")
        if type(self.options) is not tuple or any(
            type(item) is not tuple or len(item) != 2 or any(type(value) is not str for value in item)
            for item in self.options
        ):
            raise ValueError("OCR capability options must be immutable string pairs")
        if type(self.offline_enforced) is not bool:
            raise ValueError("OCR capability offline flag must be boolean")


class OcrAdapterError(RuntimeError):
    code = OcrFailureCode.ENGINE_ERROR
    retryable = False
    worker_poisoned = False


class OcrDependencyUnavailableError(OcrAdapterError):
    code = OcrFailureCode.DEPENDENCY_UNAVAILABLE


class OcrExecutableUnavailableError(OcrAdapterError):
    code = OcrFailureCode.EXECUTABLE_UNAVAILABLE


class OcrModelMissingError(OcrAdapterError):
    code = OcrFailureCode.MODEL_MISSING


class OcrModelIntegrityError(OcrAdapterError):
    code = OcrFailureCode.MODEL_INTEGRITY


class OcrLanguageUnavailableError(OcrAdapterError):
    code = OcrFailureCode.LANGUAGE_UNAVAILABLE


class OcrOfflinePolicyError(OcrAdapterError):
    code = OcrFailureCode.OFFLINE_POLICY


class OcrDeviceUnavailableError(OcrAdapterError):
    code = OcrFailureCode.DEVICE_UNAVAILABLE


class OcrRecognitionMissError(OcrAdapterError):
    code = OcrFailureCode.RECOGNITION_MISS


class OcrInferenceTimeoutError(OcrAdapterError):
    code = OcrFailureCode.TIMEOUT
    retryable = True
    worker_poisoned = True


class OcrResourceExhaustedError(OcrAdapterError):
    code = OcrFailureCode.RESOURCE_EXHAUSTED
    worker_poisoned = True


class OcrEngineExecutionError(OcrAdapterError):
    code = OcrFailureCode.ENGINE_ERROR


class OcrInvalidEngineOutputError(OcrAdapterError):
    code = OcrFailureCode.INVALID_OUTPUT


class OcrOutputTruncatedError(OcrAdapterError):
    code = OcrFailureCode.OUTPUT_TRUNCATED


class OcrWorkerDiedError(OcrAdapterError):
    code = OcrFailureCode.WORKER_DIED
    retryable = True
    worker_poisoned = True


class OcrWorkerProtocolError(OcrAdapterError):
    code = OcrFailureCode.PROTOCOL_ERROR
    worker_poisoned = True


def finite_confidence(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 1.0
    )


__all__ = [
    "OcrAdapterError",
    "OcrAttributionStatus",
    "OcrBackend",
    "OcrCapability",
    "OcrDependencyUnavailableError",
    "OcrDeviceUnavailableError",
    "OcrEngineExecutionError",
    "OcrExecutableUnavailableError",
    "OcrFailureCode",
    "OcrInferenceTimeoutError",
    "OcrInvalidEngineOutputError",
    "OcrLanguageUnavailableError",
    "OcrModelFile",
    "OcrModelIntegrityError",
    "OcrModelMissingError",
    "OcrOfflinePolicyError",
    "OcrOutputGeometry",
    "OcrOutputTruncatedError",
    "OcrRecognitionMissError",
    "OcrResource",
    "OcrResourceExhaustedError",
    "OcrWorkerDiedError",
    "OcrWorkerProtocolError",
    "finite_confidence",
]
