FROM node:22-slim AS builder

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund --fetch-retries=2 --fetch-retry-maxtimeout=20000 --fetch-timeout=30000

COPY web ./web
RUN VITE_BASE_PATH=/ npm run build

FROM nginx:1.27-alpine

COPY gateway/nginx.conf /etc/nginx/nginx.conf
COPY --from=builder /app/dist /usr/share/nginx/html
RUN nginx -t -c /etc/nginx/nginx.conf

EXPOSE 3000
