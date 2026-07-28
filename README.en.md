# OmniProxy AI

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

> **Already paying monthly for an AI account? OmniProxy brings that access to
> your applications and workflows through one local OpenAI-compatible API,
> while you monitor requests, tokens, latency and quotas.**

**Status:** Phase 1 public preview · **Experimental Builder:** disabled by
default · **Dashboard:** English / Italian / Spanish / French

[Italian documentation](README.md) ·
[Demo video guide](docs/DEMO_VIDEO.md) ·
[Security](SECURITY.md) ·
[Contributing](CONTRIBUTING.md) ·
[Issues](https://github.com/nickali00/OmniProxy-AI/issues)

## The core use case

Do you pay monthly for a ChatGPT/Codex, Google AI/Gemini or Claude account and
want to use it from your applications, automations or n8n workflows?
OmniProxy acts as the local bridge between that account and your tools:

```text
Your AI account → OmniProxy → one API for apps, backends and workflows
                                      ↓
                         usage and routing under control
```

OmniProxy exposes `/v1/chat/completions`, authenticates every application with
a local key, and uses the provider, model and reasoning profile you selected.
Clients keep the same endpoint even when you change the model behind it.

That is its calling card: **centralize supported AI accounts for your own
applications and monitor their usage in one place.**

It does not turn a consumer subscription into an official API key. Supported
official clients are executed as local adapters, and all usage remains subject
to each provider's plan, quota and terms.

## What it is for

- Bring supported AI accounts into n8n, backends and automations through one
  OpenAI-compatible endpoint.
- Automatic Ollama discovery and optional managed GPU container.
- Isolated sidecars for Codex, Gemini/Antigravity and Claude clients.
- Stable managed API profiles locked to a provider, model and reasoning level.
- Local API-key authentication with hashed secrets.
- SQLite request, token, latency and error accounting.
- Live provider availability/quota information when officially exposed.
- A multilingual local dashboard.

## Architecture

```text
n8n / application / OpenAI-compatible client
                  |
                  | Bearer sk-local-...
                  v
        FastAPI /v1/chat/completions
                  |
        routing + managed API profile
          |           |           |
       Ollama      Codex CLI   Gemini / Claude clients
          |
   local NVIDIA GPU

        SQLite: key hashes + usage metadata
```

Provider sessions live in separate Docker volumes. They are not copied into
the browser, the SQLite database or application logs.

## Quick start on Ubuntu

### Requirements

- Ubuntu 22.04 or newer.
- [Docker Engine and the Compose plugin](https://docs.docker.com/engine/install/ubuntu/).
- Git.
- NVIDIA drivers and the
  [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)
  only when running managed Ollama with GPU.

### Install

```bash
git clone https://github.com/nickali00/OmniProxy-AI.git
cd OmniProxy-AI
cp .env.example .env
```

Edit `.env` and replace the bootstrap key with a random value starting with
`sk-local-` and containing at least 32 characters:

```dotenv
BOOTSTRAP_API_KEY=sk-local-change-me-with-a-long-random-secret
```

Never commit or share `.env`.

Start the gateway while using an existing Ollama instance:

```bash
docker compose up -d --build
```

Or let the Compose profile run Ollama with NVIDIA GPU access:

```bash
docker compose --profile managed-ollama up -d --build
```

Verify:

```bash
docker compose ps
curl http://127.0.0.1:8000/healthz
```

Open `http://127.0.0.1:8000/`. If port 8000 is unavailable, change
`GATEWAY_PORT` in `.env`.

## First managed API

1. Connect a provider from **Connections**, or start Ollama.
2. Open **Models** and select a model that is currently available.
3. Select **Create API with this model**.
4. Name the API, choose its reasoning level and save it.
5. Copy the generated `sk-local-...` secret immediately. It is shown once.
6. Configure your client with:

```text
Base URL: http://127.0.0.1:8000/v1
API key:  sk-local-...
Model:    the stable slug shown by OmniProxy
```

## API example

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $OMNIPROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "your-managed-model-slug",
    "messages": [{"role": "user", "content": "Hello from OmniProxy"}]
  }'
```

For an n8n container attached to `omni-proxy-ai-network`, use
`http://gateway:8000/v1` instead of the host URL.

## Demo video

[![OmniProxy AI demo with n8n](docs/assets/demo-cover.webp)](https://github.com/nickali00/OmniProxy-AI/releases/download/v0.1.0/omniproxy-ai-demo-v0.1.0.mp4)

[Watch or download the complete demo (1:49, no audio)](https://github.com/nickali00/OmniProxy-AI/releases/download/v0.1.0/omniproxy-ai-demo-v0.1.0.mp4).
It shows a managed API being created, an n8n OpenAI node being configured, a
real request, and usage metrics updating. The
[video guide](docs/DEMO_VIDEO.md) also provides a safe storyboard for future
recordings.

## Security model

- The dashboard binds to loopback by default.
- Local API secrets are stored only as SHA-256 hashes.
- Provider brokers expose no host ports and use separate volumes.
- Containers run non-root with read-only filesystems and dropped capabilities.
- Browser authentication is restricted to allowlisted official HTTPS origins.
- Prompt and response bodies are not shown by the usage dashboard.

Do not publish the dashboard port directly on the Internet. Remote access
requires a VPN or a TLS reverse proxy with administrative authentication and
request limits.

## Experimental Builder

The Builder workspace is excluded from the public interface and its server
routes return `404` by default. Developers can enable it locally with:

```dotenv
BUILD_ENABLED=true
```

Rebuild the gateway after changing the flag.

## Tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

## Current limitations

- SQLite targets a single gateway replica.
- `tiktoken` accounting may differ from a local model's native tokenizer.
- Tool calls, multimodal inputs and `/v1/responses` are not implemented yet.
- Cloud availability depends on account access and the official client.
- Cloud SSE output is currently emitted after the headless completion.
- The legacy `reasoning-avanzato` alias still uses a mock provider.

See the [Italian documentation](README.md) for provider authentication,
managed APIs, n8n networking and detailed security notes.

## License

OmniProxy AI is licensed under the
[GNU Affero General Public License v3.0 only](LICENSE)
(`AGPL-3.0-only`). You may use, modify, and distribute it, including
commercially, subject to the license terms. If you make a modified version
available over a network, you must offer its users the corresponding source
code.

Copyright © 2026 Nicola Alì.
