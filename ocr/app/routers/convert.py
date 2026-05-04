from fastapi import APIRouter, UploadFile, File, HTTPException
import time
import tempfile
import os
import shutil
from pathlib import Path
from app.services import convert_service
from app.schemas import ConvertResponse, ConvertMeta

router = APIRouter()

@router.post("/convert", response_model=ConvertResponse)
async def convert_endpoint(file: UploadFile = File(...)):
    start_time = time.time()
    
    # Save the uploaded file temporarily
    fd, temp_path_str = tempfile.mkstemp(suffix=Path(file.filename or "").suffix or "")
    temp_path = Path(temp_path_str)
    
    try:
        with os.fdopen(fd, 'wb') as f:
            shutil.copyfileobj(file.file, f)
            
        markdown_text, meta_info = await convert_service.convert(temp_path)
        
        elapsed = int((time.time() - start_time) * 1000)
        meta_info["elapsed_ms"] = elapsed
        
        return ConvertResponse(
            markdown=markdown_text,
            meta=ConvertMeta(**meta_info)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if temp_path.exists():
            os.remove(temp_path)
