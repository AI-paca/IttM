ARG RUST_BUILD_IMAGE=rust:1.96.1-bookworm
FROM ${RUST_BUILD_IMAGE} AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config libtesseract-dev libleptonica-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY pipeline-core/Cargo.toml pipeline-core/Cargo.lock ./pipeline-core/
COPY pipeline-core/src ./pipeline-core/src
COPY ocr-runtime/Cargo.toml ocr-runtime/Cargo.lock ocr-runtime/build.rs ocr-runtime/profiles.json ./ocr-runtime/
COPY ocr-runtime/src ./ocr-runtime/src
RUN cargo build --locked --release --manifest-path ocr-runtime/Cargo.toml

FROM debian:bookworm-slim AS runtime
ARG OCR_INSTALL_CJK_FONTS=0
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl libtesseract5 tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus \
    tesseract-ocr-kaz tesseract-ocr-kir tesseract-ocr-chi-sim poppler-utils \
    && if [ "$OCR_INSTALL_CJK_FONTS" = "1" ]; then apt-get install -y --no-install-recommends fonts-noto-cjk; fi \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ittm \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin ittm
COPY --from=builder /build/ocr-runtime/target/release/ittm-ocr /usr/local/bin/ittm-ocr
ENV PORT=8000 OCR_HOST=0.0.0.0
USER ittm
EXPOSE 8000
CMD ["ittm-ocr", "serve"]
