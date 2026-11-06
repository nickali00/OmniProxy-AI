FROM node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d

ARG TARGETARCH
ARG ANTIGRAVITY_CLI_VERSION
ARG ANTIGRAVITY_CLI_SHA256_AMD64
ARG ANTIGRAVITY_CLI_SHA256_ARM64

COPY antigravity-broker/version.txt antigravity-broker/sha256-amd64.txt antigravity-broker/sha256-arm64.txt /tmp/

ENV NODE_ENV=production \
    HOME=/home/antigravity \
    BROKER_HOST=0.0.0.0 \
    BROKER_PORT=8789 \
    ANTIGRAVITY_BINARY=/usr/local/bin/antigravity \
    NO_COLOR=1

RUN apt-get update \
    && apt-get upgrade --yes --no-install-recommends \
    && rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx /opt/yarn-* /usr/local/bin/yarn /usr/local/bin/yarnpkg \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        git \
        util-linux \
    && cli_version="${ANTIGRAVITY_CLI_VERSION:-$(cat /tmp/version.txt)}" \
    && case "${TARGETARCH}" in \
        "amd64") \
            asset="agy_cli_linux_x64.tar.gz"; \
            digest="${ANTIGRAVITY_CLI_SHA256_AMD64:-$(cat /tmp/sha256-amd64.txt)}" ;; \
        "arm64") \
            asset="agy_cli_linux_arm64.tar.gz"; \
            digest="${ANTIGRAVITY_CLI_SHA256_ARM64:-$(cat /tmp/sha256-arm64.txt)}" ;; \
        *) echo "Architettura Antigravity non supportata: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --silent --show-error \
        "https://github.com/google-antigravity/antigravity-cli/releases/download/${cli_version}/${asset}" \
        --output /tmp/antigravity.tar.gz \
    && echo "${digest}  /tmp/antigravity.tar.gz" | sha256sum --check - \
    && tar --extract --gzip --file /tmp/antigravity.tar.gz --directory /tmp \
    && install --mode 0755 /tmp/antigravity /usr/local/bin/antigravity \
    && ln --symbolic /usr/local/bin/antigravity /usr/local/bin/agy \
    && rm --force /tmp/antigravity /tmp/antigravity.tar.gz \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10004 antigravity \
    && useradd \
        --system \
        --uid 10004 \
        --gid antigravity \
        --home-dir /home/antigravity \
        --create-home \
        antigravity \
    && mkdir -p /home/antigravity/.gemini /app /workspace \
    && chown -R antigravity:antigravity /home/antigravity /app /workspace

WORKDIR /app

COPY --chown=antigravity:antigravity antigravity-broker/server.mjs ./server.mjs
COPY --chown=antigravity:antigravity antigravity-broker/models.mjs ./models.mjs
COPY --chown=antigravity:antigravity antigravity-broker/headless.mjs ./headless.mjs
COPY --chown=antigravity:antigravity provider-common/runtime.mjs ./runtime.mjs

USER antigravity

EXPOSE 8789

CMD ["node", "server.mjs"]
