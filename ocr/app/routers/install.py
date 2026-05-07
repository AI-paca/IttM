from fastapi import APIRouter
import subprocess
import sys
import os

router = APIRouter()

@router.post("/v1/install-easyocr")
async def install_easyocr():
    try:
        # Check if already installed
        try:
            import easyocr
            return {"status": "already_installed"}
        except ImportError:
            pass
            
        # Run pip install easyocr
        subprocess.check_call([sys.executable, "-m", "pip", "install", "easyocr", "torch", "torchvision"])
        return {"status": "installed"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
