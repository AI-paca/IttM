ARG PYTHON_BASE_IMAGE=python:3.10-slim@sha256:fa184fce49c170a8b1032a4f752f9fe1a7e463e7f5795a3952ca275e166fa913

FROM ${PYTHON_BASE_IMAGE} AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    PYTHONPATH=/opt/ittm-python-packages \
    EASY_INSTALL_TARGET=/opt/ittm-python-packages \
    EASYOCR_MODULE_PATH=/models/easyocr

ARG OCR_INSTALL_CJK_FONTS=0

RUN set -eux; \
    apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=10 -o Acquire::https::Timeout=10 update; \
    apt-get upgrade -y --no-install-recommends; \
    apt-get install -y --no-install-recommends \
      tesseract-ocr \
      tesseract-ocr-eng \
      tesseract-ocr-rus \
      tesseract-ocr-kaz \
      tesseract-ocr-kir \
      tesseract-ocr-chi-sim \
      poppler-utils \
      libglib2.0-0; \
    if [ "$OCR_INSTALL_CJK_FONTS" = "1" ]; then \
      apt-get install -y --no-install-recommends fonts-noto-cjk; \
    fi; \
    apt-get upgrade -y --no-install-recommends; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

ARG PYTHON_REQUIREMENTS=requirements-light.txt
ARG PIP_VERSION=26.1.2
ARG SETUPTOOLS_VERSION=82.0.1
ARG WHEEL_VERSION=0.47.0
COPY requirements*.txt ./

RUN python -m pip install \
      --no-cache-dir \
      --no-compile \
      --retries 2 \
      --default-timeout 60 \
      "pip==$PIP_VERSION" \
      "setuptools==$SETUPTOOLS_VERSION" \
      "wheel==$WHEEL_VERSION" \
    && python -m pip install \
      --no-cache-dir \
      --no-compile \
      --retries 2 \
      --default-timeout 60 \
      -r "$PYTHON_REQUIREMENTS"

FROM base AS app-base
COPY app ./app

FROM app-base AS test
COPY pyproject.toml ./
COPY .flake8 ./
COPY tests ./tests

FROM app-base AS runtime
RUN groupadd --gid 10001 ittm \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin ittm \
    && mkdir -p "$EASY_INSTALL_TARGET" "$EASYOCR_MODULE_PATH" \
    && chown -R ittm:ittm "$EASY_INSTALL_TARGET" "$EASYOCR_MODULE_PATH"

EXPOSE 8000
USER ittm

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
