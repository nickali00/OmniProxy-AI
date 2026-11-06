"""Real local-model checks with temporary profiles, database and token policies."""

import asyncio
import json
import logging
import tempfile
from pathlib import Path

import httpx

from app.config import settings
from app.database import create_gateway_api
from app.main import app


async def verify():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="omniproxy-limits-e2e-") as temporary:
        settings.database_url = ""
        settings.admin_auth_required = False
        settings.dashboard_auth_required = False
        settings.require_https = False
        settings.otel_enabled = False
        settings.database_path = Path(temporary) / "gateway.sqlite3"
        settings.bootstrap_api_key = "sk-local-limit-test-key-with-thirty-two-characters"
        settings.ollama_base_url = "http://127.0.0.1:11434"
        settings.ollama_model = "qwen3.5:2b"
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", timeout=300) as client:
            async def profile(name, limits):
                record, key = await create_gateway_api(name=name, provider="ollama", model=settings.ollama_model, reasoning_effort="off", limits=limits)
                return record.slug, {"Authorization": f"Bearer {key}"}

            model, headers = await profile("Input cap", {"input_tokens": 32})
            rejected = await client.post("/v1/chat/completions", headers=headers, json={"model": model, "messages": [{"role": "user", "content": "oversized text " * 50}]})
            assert rejected.status_code == 400 and rejected.json()["error"]["code"] == "input_token_limit_exceeded"
            model, headers = await profile("Output cap", {"output_tokens": 8, "token_budget": 5000, "budget_period": "daily"})
            output = await client.post("/v1/chat/completions", headers=headers, json={"model": model, "messages": [{"role": "user", "content": "Write a long story about a blue bird."}], "max_completion_tokens": 128})
            assert output.status_code == 200, output.status_code
            assert 0 < output.json()["usage"]["completion_tokens"] <= 8
            model, headers = await profile("Context compaction", {"context_tokens": 2048, "output_tokens": 256, "context_mode": "summarize", "token_budget": 20000, "budget_period": "monthly"})
            compacted = await client.post("/v1/chat/completions", headers=headers, json={"model": model, "messages": [
                {"role": "system", "content": "Answer briefly. The project color is BLUE."},
                {"role": "user", "content": "Historical measurements, all completed. " * 500},
                {"role": "assistant", "content": "Measurements recorded. The project color remains BLUE."},
                {"role": "user", "content": "What is the project color? Answer one word."},
            ]})
            assert compacted.status_code == 200, compacted.text
            assert int(compacted.headers["X-OmniProxy-Context-Compactions"]) > 0
            assert "blue" in compacted.json()["choices"][0]["message"]["content"].lower()
            usage = (await client.get("/v1/usage", headers=headers)).json()["usage"]
            assert usage["reserved_tokens"] == 0 and usage["used_tokens"] == compacted.json()["usage"]["total_tokens"]
            print(json.dumps({"scope": "source_asgi_temporary_database", "model": settings.ollama_model, "input_rejection": True, "output_cap": True, "real_context_compaction": True, "summary_budget_accounted": True, "passed": True}, indent=2))


if __name__ == "__main__":
    asyncio.run(verify())
