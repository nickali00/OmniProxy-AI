FROM node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d

ARG CODEX_CLI_VERSION=

ENV NODE_ENV=production \
    BROKER_HOST=0.0.0.0 \
    BROKER_PORT=8788

COPY codex-broker/version.txt /app/codex-version.txt

RUN apt-get update \
    && apt-get upgrade --yes --no-install-recommends \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && version="${CODEX_CLI_VERSION:-$(cat /app/codex-version.txt)}" \
    && npm install --global "@openai/codex@${version}" \
    && npm cache clean --force \
    && rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx /opt/yarn-* /usr/local/bin/yarn /usr/local/bin/yarnpkg \
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
COPY --chown=codex:codex codex-broker/client-tools.mjs ./client-tools.mjs
COPY --chown=codex:codex codex-broker/tool-policy.mjs ./tool-policy.mjs
COPY --chown=codex:codex provider-common/runtime.mjs ./runtime.mjs

USER codex

EXPOSE 8788

CMD ["node", "server.mjs"]
