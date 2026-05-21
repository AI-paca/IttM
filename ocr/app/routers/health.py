import platform
import shutil

from fastapi import APIRouter

from app.schemas import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
@router.get("/v1/health", response_model=HealthResponse)
def health_endpoint():
    return HealthResponse(ok=True, service="Python OCR Service")


@router.get("/readiness")
@router.get("/v1/readiness")
def readiness_endpoint():
    checks = {
        "tesseract": shutil.which("tesseract") is not None,
        "pdftoppm": shutil.which("pdftoppm") is not None,
        "pytesseract": _module_available("pytesseract"),
        "pdf2image": _module_available("pdf2image"),
        "opencv": _module_available("cv2"),
    }

    return {"ready": all(checks.values()), "checks": checks}


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


@router.get("/v1/capabilities")
def capabilities_endpoint():
    try:
        from app.engines.easyocr_engine import EasyOcrEngine
        from app.engines.tesseract_engine import TesseractEngine

        tess = TesseractEngine()
        easy = EasyOcrEngine(download_enabled=False)

        return {
            "engines": [
                tess.info(),
                easy.info(),
            ]
        }
    except Exception as e:
        return {"error": str(e), "engines": []}


@router.get("/diagnostics")
@router.get("/v1/diagnostics")
def diagnostics_endpoint():
    try:
        import psutil

        mem = psutil.virtual_memory()
        memory_gb = round(mem.total / (1024**3), 2)
        used_gb = round(mem.used / (1024**3), 2)
        cpu_cores = psutil.cpu_count(logical=True)
    except Exception:
        memory_gb = 0
        used_gb = 0
        cpu_cores = 0

    gpus = []
    gpu_error = None
    torch_error = None
    torch_available = False
    easyocr_available = _module_available("easyocr")

    try:
        import torch

        torch_available = True
        try:
            if torch.cuda.is_available():
                hip_version = getattr(torch.version, "hip", None)
                device_name = "Unknown CUDA Device"
                try:
                    device_name = torch.cuda.get_device_name(0)
                except Exception as e:
                    gpu_error = f"get_device_name(0) failed: {str(e)}"

                if hip_version:
                    gpus.append({"type": "rocm", "name": f"ROCm ({device_name})", "version": hip_version})
                else:
                    gpus.append({"type": "cuda", "name": f"CUDA ({device_name})", "version": torch.version.cuda})

            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                gpus.append({"type": "mps", "name": "Apple Metal (MPS)", "version": "MPS"})
        except Exception as e:
            gpu_error = f"Torch GPU check failed: {str(e)}"
    except ImportError:
        gpu_error = None
    except Exception as e:
        torch_error = f"Torch import failed: {str(e)}"

    try:
        import onnxruntime as ort

        providers = ort.get_available_providers()
        if "OpenVINOExecutionProvider" in providers:
            gpus.append({"type": "openvino", "name": "Intel OpenVINO", "version": "ORT"})
        if "DmlExecutionProvider" in providers:
            gpus.append({"type": "dml", "name": "DirectML", "version": "ORT"})
        if "TensorrtExecutionProvider" in providers:
            gpus.append({"type": "tensorrt", "name": "Nvidia TensorRT", "version": "ORT"})
    except Exception:
        pass

    # If no GPUs detected via torch/ort but pynvml might see something
    if not gpus:
        try:
            import pynvml

            pynvml.nvmlInit()
            device_count = pynvml.nvmlDeviceGetCount()
            for i in range(device_count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode("utf-8")
                gpus.append({"type": "nvml", "name": f"NVIDIA {name} (NVML)", "version": "detected"})
            pynvml.nvmlShutdown()
        except Exception:
            pass

    return {
        "python_version": platform.python_version(),
        "system": platform.system(),
        "memory_total_gb": memory_gb,
        "memory_used_gb": used_gb,
        "gpus": gpus,
        "gpu_error": gpu_error,
        "torch_error": torch_error,
        "torch_available": torch_available,
        "easyocr_available": easyocr_available,
        "cpu_cores": cpu_cores,
    }
