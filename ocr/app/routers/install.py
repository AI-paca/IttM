from fastapi import APIRouter
import subprocess
import sys

router = APIRouter()

@router.post("/install-easyocr")
@router.post("/v1/install-easyocr")
async def install_easyocr():
    try:
        # Check if already installed
        try:
            import easyocr
            return {"status": "already_installed"}
        except ImportError:
            pass
            
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "easyocr", "torch", "torchvision"],
            capture_output=True,
            check=False,
            text=True,
            timeout=1800,
        )
        if result.returncode != 0:
            return {
                "status": "error",
                "message": (result.stderr or result.stdout or "pip install failed").strip()[-1000:],
            }
        return {"status": "installed"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "pip install timed out after 30 minutes"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
