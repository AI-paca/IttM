import json
import queue
import threading
import time

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.pipeline_flags import ensure_flag_overrides_allowed, pipeline_flags_payload
from app.pipeline_config import resolve_pipeline_profile
from app.schemas import ConvertResponse, ConvertMeta
from app.services import convert_service
from app.upload_limits import read_upload_limited

router = APIRouter()
STREAM_HEARTBEAT_SECONDS = 15.0
_STREAM_END = object()


def _stream_conversion(content, filename, engine_type, pipeline, pdf_mode):
    start_time = time.time()
    events = queue.Queue()
    stopped = threading.Event()

    def publish(event):
        delivered = threading.Event()
        events.put((event, delivered))
        while not delivered.wait(0.1):
            if stopped.is_set():
                return False
        return True

    def produce():
        try:
            for event in convert_service.iter_convert_bytes(
                content,
                filename=filename,
                engine_type=engine_type,
                pipeline_profile=pipeline,
                pdf_mode=pdf_mode,
            ):
                if stopped.is_set():
                    return
                if not publish(event):
                    return
        except Exception as exc:
            import traceback

            traceback.print_exc()
            if not stopped.is_set():
                publish({"type": "error", "detail": str(exc)})
        finally:
            if not stopped.is_set():
                events.put(_STREAM_END)

    threading.Thread(target=produce, daemon=True).start()

    try:
        while True:
            try:
                event = events.get(timeout=STREAM_HEARTBEAT_SECONDS)
            except queue.Empty:
                yield json.dumps(
                    {"type": "progress", "stage": "ocr"},
                    ensure_ascii=False,
                ) + "\n"
                continue

            if event is _STREAM_END:
                return
            event, delivered = event
            if event["type"] == "complete":
                event["meta"]["elapsed_ms"] = int((time.time() - start_time) * 1000)
            yield json.dumps(event, ensure_ascii=False) + "\n"
            delivered.set()
    finally:
        stopped.set()


@router.post("/convert", response_model=ConvertResponse)
@router.post("/v1/convert", response_model=ConvertResponse)
async def convert_endpoint(
    file: UploadFile = File(...),
    engine_type: str = Query(
        "auto", description="Engine type: auto, tesseract, or easyocr"
    ),
    pipeline_profile: str | None = Query(
        None, description="High-level OCR pipeline profile name"
    ),
    pipeline_flags: str | None = Query(
        None, description="Reserved pipeline flag overrides. Disabled for now."
    ),
    pdf_mode: str = Query(
        "auto",
        description="PDF handling: auto uses a trustworthy text layer; raster forces page OCR.",
    ),
):
    print(
        f"[CONVERT] Received request: {file.filename} (content_type={file.content_type}), engine={engine_type}"
    )
    # This function handles both /convert and /v1/convert
    start_time = time.time()

    try:
        ensure_flag_overrides_allowed(pipeline_flags)
        content = await read_upload_limited(file)
        pipeline = resolve_pipeline_profile(engine_type, pipeline_profile)
        normalized_pdf_mode = convert_service.normalize_pdf_mode(pdf_mode)
        markdown_text, meta_info = await run_in_threadpool(
            convert_service.convert_bytes,
            content,
            filename=file.filename or "upload",
            engine_type=engine_type,
            pipeline_profile=pipeline,
            pdf_mode=normalized_pdf_mode,
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


@router.post("/convert/stream")
@router.post("/v1/convert/stream")
async def convert_stream_endpoint(
    file: UploadFile = File(...),
    engine_type: str = Query(
        "auto", description="Engine type: auto, tesseract, or easyocr"
    ),
    pipeline_profile: str | None = Query(
        None, description="High-level OCR pipeline profile name"
    ),
    pipeline_flags: str | None = Query(
        None, description="Reserved pipeline flag overrides. Disabled for now."
    ),
    pdf_mode: str = Query(
        "auto",
        description="PDF handling: auto uses a trustworthy text layer; raster forces page OCR.",
    ),
):
    print(
        f"[CONVERT] Received streaming request: {file.filename} "
        f"(content_type={file.content_type}), engine={engine_type}"
    )
    try:
        ensure_flag_overrides_allowed(pipeline_flags)
        content = await read_upload_limited(file)
        pipeline = resolve_pipeline_profile(engine_type, pipeline_profile)
        normalized_pdf_mode = convert_service.normalize_pdf_mode(pdf_mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OverflowError as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    finally:
        await file.close()

    return StreamingResponse(
        _stream_conversion(
            content,
            file.filename or "upload",
            engine_type,
            pipeline,
            normalized_pdf_mode,
        ),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/pipeline/flags")
@router.get("/v1/pipeline/flags")
async def pipeline_flags_endpoint():
    return pipeline_flags_payload()
