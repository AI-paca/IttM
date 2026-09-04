ARG NODE_IMAGE=node:22-slim@sha256:d9f850096136edbc402debdd8729579a288aac64574ada0ff4db26b6ae58b0b2
ARG NGINX_IMAGE=nginx:1.31.3-alpine@sha256:4a73073bd557c65b759505da037898b61f1be6cbcc3c2c3aeac22d2a470c1752
ARG RUST_BUILD_IMAGE=rust@sha256:1f0dbad1df66647807e6952d1db85d0b2bda7606cb2139d82517e4f009967376

FROM ${RUST_BUILD_IMAGE} AS pipeline-core-wasm-builder

WORKDIR /core
COPY pipeline-core/Cargo.toml pipeline-core/Cargo.lock ./
COPY pipeline-core/src ./src
RUN rustup target add wasm32-unknown-unknown \
    && cargo build --locked --release --target wasm32-unknown-unknown

FROM ${NODE_IMAGE} AS builder

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund --fetch-retries=5 --fetch-retry-mintimeout=10000 --fetch-retry-maxtimeout=120000 --fetch-timeout=120000

COPY scripts/models/download-browser-tessdata.sh \
  scripts/models/download-browser-text-validator.sh \
  ./scripts/models/
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && bash scripts/models/download-browser-tessdata.sh \
    && bash scripts/models/download-browser-text-validator.sh \
    && rm -rf /var/lib/apt/lists/*

COPY web ./web
COPY --from=pipeline-core-wasm-builder \
  /core/target/wasm32-unknown-unknown/release/ittm_pipeline_core.wasm \
  ./web/public/wasm/ittm_pipeline_core.wasm
RUN VITE_BASE_PATH=/ npm run build:web

FROM ${NGINX_IMAGE}

ARG SECURITY_REFRESH=manual
RUN echo "$SECURITY_REFRESH" >/dev/null \
    && apk upgrade --no-cache

ENV NGINX_LISTEN_PORT=80
ENV GATEWAY_HOSTNAME=gateway
ENV GATEWAY_INTERNAL_PORT=3000

COPY gateway/nginx.conf /etc/nginx/templates/default.conf.template
COPY pipeline-core/Cargo.toml pipeline-core/Cargo.lock /usr/share/ittm/sbom/pipeline-core/
COPY --from=builder /app/dist /usr/share/nginx/html
RUN envsubst '$NGINX_LISTEN_PORT $GATEWAY_HOSTNAME $GATEWAY_INTERNAL_PORT' \
    < /etc/nginx/templates/default.conf.template \
    > /etc/nginx/conf.d/default.conf \
    && nginx -t

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
