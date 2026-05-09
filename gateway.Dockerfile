FROM node:22-slim AS builder

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
RUN npm run build

FROM node:22-slim
WORKDIR /app
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/dist ./dist
COPY --from=builder /app/gateway ./gateway

ENV PORT=3000
ENV NODE_ENV=production
EXPOSE 3000

CMD ["npx", "tsx", "gateway/src/adapters/node.ts"]
