ARG NODE_RUNTIME_IMAGE=node:22-alpine@sha256:ab07539e0988b63558ff621f5fbe1077054c39d9809112974fb79993949d41cd

FROM ${NODE_RUNTIME_IMAGE} AS runtime

WORKDIR /app
ENV PORT=8000

RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
    && rm -f \
      /usr/local/bin/corepack \
      /usr/local/bin/npm \
      /usr/local/bin/npx \
      /usr/local/bin/pnpm \
      /usr/local/bin/yarn

COPY scripts/ci/ocr-smoke-stub.mjs ./ocr-smoke-stub.mjs

EXPOSE 8000
USER node

CMD ["node", "ocr-smoke-stub.mjs"]
