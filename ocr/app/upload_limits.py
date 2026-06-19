import os

from fastapi import UploadFile

DEFAULT_MAX_UPLOAD_BYTES = 128 * 1024 * 1024
UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


def max_upload_bytes() -> int:
    raw_value = os.environ.get("OCR_MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("OCR_MAX_UPLOAD_BYTES must be an integer") from exc
    if value < 0:
        raise RuntimeError("OCR_MAX_UPLOAD_BYTES must not be negative")
    return value


async def read_upload_limited(upload: UploadFile, limit: int | None = None) -> bytes:
    max_bytes = limit if limit is not None else max_upload_bytes()
    content = bytearray()

    while True:
        read_size = UPLOAD_READ_CHUNK_BYTES
        if max_bytes:
            remaining = max_bytes - len(content)
            read_size = min(read_size, remaining + 1)
        chunk = await upload.read(read_size)
        if not chunk:
            break
        content.extend(chunk)
        if max_bytes and len(content) > max_bytes:
            raise OverflowError(f"File exceeds the {max_bytes} byte upload limit")

    if not content:
        raise ValueError("Uploaded file is empty")
    return bytes(content)
