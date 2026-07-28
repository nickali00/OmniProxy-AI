FROM node:22-bookworm-slim

ARG CODEX_CLI_VERSION=0.145.0

ENV NODE_ENV=production \
    BROKER_HOST=0.0.0.0 \
    BROKER_PORT=8788

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@openai/codex@${CODEX_CLI_VERSION}" \
    && npm cache clean --force \
    && groupadd --system --gid 10003 codex \
    && useradd \
        --system \
        --uid 10003 \
        --gid codex \
        --home-dir /home/codex \
        --create-home \
        codex \
    && mkdir -p /home/codex/.codex /app /workspace \
    && chown -R codex:codex /home/codex /app /workspace

WORKDIR /app

COPY --chown=codex:codex codex-broker/server.mjs ./server.mjs
COPY --chown=codex:codex provider-common/runtime.mjs ./runtime.mjs

USER codex

EXPOSE 8788

CMD ["node", "server.mjs"]
