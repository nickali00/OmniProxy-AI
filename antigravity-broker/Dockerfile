FROM node:22-bookworm-slim

ARG TARGETARCH
ARG ANTIGRAVITY_CLI_VERSION=1.1.7
ARG ANTIGRAVITY_CLI_SHA256_AMD64=946cd06258d0ede72d0311550c914315798821f6a397f53ac760919826a19af4
ARG ANTIGRAVITY_CLI_SHA256_ARM64=0d6d488851745e80e69b8935d063e742945811b47111994b1a6dbd27df3010d5

ENV NODE_ENV=production \
    HOME=/home/antigravity \
    BROKER_HOST=0.0.0.0 \
    BROKER_PORT=8789 \
    ANTIGRAVITY_BINARY=/usr/local/bin/antigravity \
    NO_COLOR=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        git \
        util-linux \
    && case "${TARGETARCH}" in \
        "amd64") \
            asset="agy_cli_linux_x64.tar.gz"; \
            digest="${ANTIGRAVITY_CLI_SHA256_AMD64}" ;; \
        "arm64") \
            asset="agy_cli_linux_arm64.tar.gz"; \
            digest="${ANTIGRAVITY_CLI_SHA256_ARM64}" ;; \
        *) echo "Architettura Antigravity non supportata: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --silent --show-error \
        "https://github.com/google-antigravity/antigravity-cli/releases/download/${ANTIGRAVITY_CLI_VERSION}/${asset}" \
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
COPY --chown=antigravity:antigravity provider-common/runtime.mjs ./runtime.mjs

USER antigravity

EXPOSE 8789

CMD ["node", "server.mjs"]
