FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.218

ENV NODE_ENV=production \
    CLAUDE_CONFIG_DIR=/home/claude/.claude \
    BROKER_HOST=0.0.0.0 \
    BROKER_PORT=8787

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
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
