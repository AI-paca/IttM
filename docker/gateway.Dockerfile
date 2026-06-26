ARG NODE_BUILD_IMAGE=node:22-slim@sha256:d9f850096136edbc402debdd8729579a288aac64574ada0ff4db26b6ae58b0b2
ARG NODE_RUNTIME_IMAGE=node:22-alpine@sha256:ab07539e0988b63558ff621f5fbe1077054c39d9809112974fb79993949d41cd

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

RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
    && rm -f \
      /usr/local/bin/corepack \
      /usr/local/bin/npm \
      /usr/local/bin/npx \
      /usr/local/bin/pnpm \
      /usr/local/bin/yarn

COPY --from=builder /app/dist ./dist

EXPOSE 3000
USER node

CMD ["node", "dist/server.cjs"]
