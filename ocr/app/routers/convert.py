import time

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from starlette.concurrency import run_in_threadpool

from app.pipeline_config import resolve_pipeline_profile
from app.schemas import ConvertResponse, ConvertMeta
from app.services import convert_service
from app.upload_limits import read_upload_limited

router = APIRouter()


@router.post("/convert", response_model=ConvertResponse)
@router.post("/v1/convert", response_model=ConvertResponse)
async def convert_endpoint(
    file: UploadFile = File(...),
    engine_type: str = Query("auto", description="Engine type: auto, tesseract, or easyocr"),
    pipeline_profile: str | None = Query(None, description="High-level OCR pipeline profile name"),
):
    print(f"[CONVERT] Received request: {file.filename} (content_type={file.content_type}), engine={engine_type}")
    # This function handles both /convert and /v1/convert
    start_time = time.time()

    try:
        content = await read_upload_limited(file)
        pipeline = resolve_pipeline_profile(engine_type, pipeline_profile)
        markdown_text, meta_info = await run_in_threadpool(
            convert_service.convert_bytes,
            content,
            filename=file.filename or "upload",
            engine_type=engine_type,
            pipeline_profile=pipeline,
        )

        elapsed = int((time.time() - start_time) * 1000)
        meta_info["elapsed_ms"] = elapsed

        return ConvertResponse(
            markdown=markdown_text,
            meta=ConvertMeta(**meta_info),
        )
    except ValueError as ve:
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(ve))
    except OverflowError as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")
    finally:
        await file.close()
