FROM python:3.10-slim AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

ARG OCR_INSTALL_CJK_FONTS=0

RUN set -eux; \
    apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=10 -o Acquire::https::Timeout=10 update; \
    apt-get install -y --no-install-recommends \
      tesseract-ocr \
      tesseract-ocr-eng \
      tesseract-ocr-rus \
      tesseract-ocr-chi-sim \
      poppler-utils \
      libglib2.0-0; \
    if [ "$OCR_INSTALL_CJK_FONTS" = "1" ]; then \
      apt-get install -y --no-install-recommends fonts-noto-cjk; \
    fi; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

ARG PYTHON_REQUIREMENTS=requirements-light.txt
COPY requirements*.txt ./

RUN pip install --no-cache-dir --no-compile --retries 2 --default-timeout 60 -r "$PYTHON_REQUIREMENTS"

FROM base AS app-base
COPY app ./app

FROM app-base AS test
COPY pyproject.toml ./
COPY .flake8 ./
COPY tests ./tests

FROM app-base AS runtime

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
