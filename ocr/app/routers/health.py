from fastapi import APIRouter
from app.schemas import HealthResponse
import platform

router = APIRouter()

@router.get("/health", response_model=HealthResponse)
def health_endpoint():
    return HealthResponse(ok=True, service="Python OCR Service")

@router.get("/readiness")
def readiness_endpoint():
    # If a real engine check was needed to be 'ready'
    # For now, it's just basic service readiness
    return {"ready": True}

@router.get("/v1/capabilities")
def capabilities_endpoint():
    try:
        from app.engines.tesseract_engine import TesseractEngine
        from app.engines.easyocr_engine import EasyOcrEngine
        
        tess = TesseractEngine()
        easy = EasyOcrEngine()
        
        return {
            "engines": [
                tess.info(),
                easy.info()
            ]
        }
    except Exception as e:
        return {"error": str(e), "engines": []}

@router.get("/diagnostics")
def diagnostics_endpoint():
    try:
        import psutil
        mem = psutil.virtual_memory()
        memory_gb = round(mem.total / (1024**3), 2)
        used_gb = round(mem.used / (1024**3), 2)
        cpu_cores = psutil.cpu_count(logical=True)
    except:
        memory_gb = 0
        used_gb = 0
        cpu_cores = 0
    
    gpus = []
    gpu_error = None
    try:
        import torch
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
        gpu_error = "Torch not installed"
    except Exception as e:
        gpu_error = f"Torch import failed: {str(e)}"

    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        if "OpenVINOExecutionProvider" in providers:
            gpus.append({"type": "openvino", "name": "Intel OpenVINO", "version": "ORT"})
        if "DmlExecutionProvider" in providers:
            gpus.append({"type": "dml", "name": "DirectML", "version": "ORT"})
        if "TensorrtExecutionProvider" in providers:
            gpus.append({"type": "tensorrt", "name": "Nvidia TensorRT", "version": "ORT"})
    except:
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
        except:
            pass

    return {
        "python_version": platform.python_version(),
        "system": platform.system(),
        "memory_total_gb": memory_gb,
        "memory_used_gb": used_gb,
        "gpus": gpus,
        "gpu_error": gpu_error,
        "cpu_cores": cpu_cores,
    }
