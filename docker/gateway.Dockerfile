ARG NODE_IMAGE=node:22-slim

FROM ${NODE_IMAGE} AS builder

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund --fetch-retries=5 --fetch-retry-mintimeout=10000 --fetch-retry-maxtimeout=120000 --fetch-timeout=120000
COPY server.ts tsconfig.json ./
COPY gateway ./gateway
RUN npm run build:server && npm prune --omit=dev --no-audit --no-fund

FROM ${NODE_IMAGE}
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000

COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/dist ./dist

EXPOSE 3000
USER node

CMD ["node", "dist/server.js"]
