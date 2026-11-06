"""Real local deployments, failover, SSE and opt-in vision with temporary DB."""

import asyncio
import json
import logging
import socket
import tempfile
from pathlib import Path

import httpx

from app.config import settings
from app.database import create_gateway_api
from app.main import app
from scripts.verify_baseline import red_image


async def verify():
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        unused_port = reservation.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="omniproxy-deployments-") as temporary:
        settings.database_url = ""
        settings.admin_auth_required = False
        settings.dashboard_auth_required = False
        settings.require_https = False
        settings.otel_enabled = False
        settings.database_path = Path(temporary) / "gateway.sqlite3"
        settings.bootstrap_api_key = "sk-local-deployments-test-key-with-thirty-two-characters"
        settings.upstream_base_url = ""
        settings.upstream_model = ""
        settings.upstream_deployments_json = json.dumps([
            {"id": "unreachable", "base_url": f"http://127.0.0.1:{unused_port}/v1", "model": "qwen3.5:2b", "fallbacks": ["local"], "fallback_on": ["connect_error"]},
            {"id": "local", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3.5:2b"},
            {"id": "vision", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3-vl:latest", "images": True},
        ])
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", timeout=180) as client:
            profile, key = await create_gateway_api(name="Real failover", provider="openai_compatible", model="upstream:unreachable", reasoning_effort="auto", limits={"output_tokens": 32, "token_budget": 10000, "budget_period": "daily"})
            auth = {"Authorization": f"Bearer {key}"}
            body = {"model": profile.slug, "messages": [{"role": "user", "content": "Answer only OK."}], "max_completion_tokens": 32}
            normal = await client.post("/v1/chat/completions", headers=auth, json=body)
            assert normal.status_code == 200, normal.text
            assert "ok" in normal.json()["choices"][0]["message"]["content"].lower()
            assert normal.headers["X-OmniProxy-Deployment"] == "local" and normal.headers["X-OmniProxy-Fallback"] == "true"
            streamed = await client.post("/v1/chat/completions", headers=auth, json={**body, "stream": True, "stream_options": {"include_usage": True}})
            assert streamed.status_code == 200 and streamed.text.endswith("data: [DONE]\n\n")
            assert "OK" in streamed.text.upper()
            assert '"deployment": "local"' in streamed.text and '"fallback": true' in streamed.text
            blocked = await client.post("/v1/chat/completions", headers=auth, json={**body, "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]})
            assert blocked.status_code == 502 and blocked.json()["error"]["code"] == "upstream_unreachable"
            vision, key = await create_gateway_api(name="Real upstream vision", provider="openai_compatible", model="upstream:vision", reasoning_effort="auto")
            response = await client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={"model": vision.slug, "messages": [{"role": "user", "content": [{"type": "text", "text": "What is the dominant color? Answer only RED or BLUE."}, {"type": "image_url", "image_url": {"url": red_image()}}]}], "max_completion_tokens": 64})
            assert response.status_code == 200, response.text
            assert "red" in response.json()["choices"][0]["message"]["content"].lower()
            assert (await client.get("/v1/usage", headers=auth)).json()["usage"]["reserved_tokens"] == 0
            print(json.dumps({"scope": "source_gateway_temporary_database", "real_ollama_text_and_sse": True, "failover_after_connection_refusal": True, "tools_no_replay": True, "upstream_vision": True, "passed": True}, indent=2))


if __name__ == "__main__":
    asyncio.run(verify())
