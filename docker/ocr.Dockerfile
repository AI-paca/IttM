FROM python:3.10-slim AS wheels

ARG OCR_REQUIREMENTS=requirements-ci.txt

WORKDIR /build

COPY requirements*.txt ./

RUN python -m pip wheel \
    --disable-pip-version-check \
    --no-cache-dir \
    --retries 2 \
    --default-timeout 60 \
    --wheel-dir /wheels \
    -r "${OCR_REQUIREMENTS}" \
    && cp "${OCR_REQUIREMENTS}" /wheels/requirements.txt

FROM python:3.10-slim AS runtime

ARG OCR_CONTAINER_PORT=8000

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=${OCR_CONTAINER_PORT}

RUN apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=10 -o Acquire::https::Timeout=10 update \
    && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        libgl1 \
        libglib2.0-0 \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        tesseract-ocr-eng \
        tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=wheels /wheels /wheels
RUN python -m pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    --no-index \
    --find-links=/wheels \
    -r /wheels/requirements.txt \
    && rm -rf /wheels

COPY . .

EXPOSE ${OCR_CONTAINER_PORT}

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
