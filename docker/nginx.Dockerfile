ARG NODE_IMAGE=node:22-slim@sha256:d9f850096136edbc402debdd8729579a288aac64574ada0ff4db26b6ae58b0b2
ARG NGINX_IMAGE=nginx:1.31.3-alpine@sha256:4a73073bd557c65b759505da037898b61f1be6cbcc3c2c3aeac22d2a470c1752

FROM ${NODE_IMAGE} AS builder

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund --fetch-retries=5 --fetch-retry-mintimeout=10000 --fetch-retry-maxtimeout=120000 --fetch-timeout=120000

COPY web ./web
RUN VITE_BASE_PATH=/ npm run build:web

FROM ${NGINX_IMAGE}

RUN apk upgrade --no-cache

ENV NGINX_LISTEN_PORT=80
ENV GATEWAY_HOSTNAME=gateway
ENV GATEWAY_INTERNAL_PORT=3000

COPY gateway/nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=builder /app/dist /usr/share/nginx/html
RUN envsubst '$NGINX_LISTEN_PORT $GATEWAY_HOSTNAME $GATEWAY_INTERNAL_PORT' \
    < /etc/nginx/templates/default.conf.template \
    > /etc/nginx/conf.d/default.conf \
    && nginx -t

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
