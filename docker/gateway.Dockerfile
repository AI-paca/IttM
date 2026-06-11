ARG NODE_BUILD_IMAGE=node:22-slim
ARG NODE_RUNTIME_IMAGE=alpine:3.21

FROM ${NODE_BUILD_IMAGE} AS builder

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund --fetch-retries=5 --fetch-retry-mintimeout=10000 --fetch-retry-maxtimeout=120000 --fetch-timeout=120000
COPY server.ts tsconfig.json ./
COPY gateway ./gateway
RUN npm run build:server:standalone

FROM ${NODE_RUNTIME_IMAGE}
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000

RUN apk add --no-cache nodejs \
    && addgroup -S node \
    && adduser -S -G node node

COPY --from=builder /app/dist ./dist

EXPOSE 3000
USER node

CMD ["node", "dist/server.cjs"]
