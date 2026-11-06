FROM node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d

ARG CLAUDE_CODE_VERSION=2.1.218

ENV NODE_ENV=production \
    CLAUDE_CONFIG_DIR=/home/claude/.claude \
    BROKER_HOST=0.0.0.0 \
    BROKER_PORT=8787

RUN apt-get update \
    && apt-get upgrade --yes --no-install-recommends \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
    && rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx /opt/yarn-* /usr/local/bin/yarn /usr/local/bin/yarnpkg \
    && groupadd --system --gid 10002 claude \
    && useradd \
        --system \
        --uid 10002 \
        --gid claude \
        --home-dir /home/claude \
        --create-home \
        claude \
    && mkdir -p /home/claude/.claude /app \
    && chown -R claude:claude /home/claude /app

WORKDIR /app

COPY --chown=claude:claude claude-broker/server.mjs ./server.mjs
COPY --chown=claude:claude provider-common/runtime.mjs ./runtime.mjs

USER claude

EXPOSE 8787

CMD ["node", "server.mjs"]
