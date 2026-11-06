"""Verify configured vs loaded context and real compaction on local Ollama."""

import asyncio
import json
import logging
import tempfile
from pathlib import Path

import httpx

from app.config import settings
from app.database import create_gateway_api
from app.main import _ollama_catalog, app


async def verify():
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    model = "qwen3.5:2b"
    async with httpx.AsyncClient(base_url="http://127.0.0.1:11434", timeout=180) as ollama:
        loaded = (await ollama.get("/api/ps")).json().get("models", [])
        previous = next((item.get("context_length") for item in loaded if item.get("name") == model), None)
        try:
            with tempfile.TemporaryDirectory(prefix="omniproxy-context-") as temporary:
                settings.database_url = ""
                settings.admin_auth_required = False
                settings.dashboard_auth_required = False
                settings.require_https = False
                settings.otel_enabled = False
                settings.database_path = Path(temporary) / "gateway.sqlite3"
                settings.bootstrap_api_key = "sk-local-context-test-key-with-thirty-two-characters"
                settings.ollama_base_url = "http://127.0.0.1:11434"
                settings.ollama_model = model
                settings.ollama_context_tokens = 4096
                async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", timeout=180) as client:
                    record, key = await create_gateway_api(name="Physical reject", provider="ollama", model=model, reasoning_effort="off")
                    body = {"model": record.slug, "messages": [{"role": "system", "content": "The project color is BLUE. Answer questions briefly."}, {"role": "user", "content": "Past measurements are completed. " * 1500}, {"role": "assistant", "content": "The project color is BLUE."}, {"role": "user", "content": "What is the project color? Answer one word."}], "max_completion_tokens": 128}
                    rejected = await client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
                    assert rejected.status_code == 400 and rejected.json()["error"]["code"] == "model_context_limit_exceeded"
                    record, key = await create_gateway_api(name="Physical summary", provider="ollama", model=model, reasoning_effort="off", limits={"context_tokens": 8192, "output_tokens": 128, "context_mode": "summarize"})
                    body["model"] = record.slug
                    auth = {"Authorization": f"Bearer {key}"}
                    response = await client.post("/v1/chat/completions", headers=auth, json=body)
                    assert response.status_code == 200, response.text
                    assert response.headers["X-OmniProxy-Model-Context"] == "4096"
                    assert int(response.headers["X-OmniProxy-Context-Compactions"]) > 0
                    assert "blue" in response.json()["choices"][0]["message"]["content"].lower()
                    catalog = await _ollama_catalog(app)
                    reported = next(item for item in catalog["models"] if item["id"] == model)
                    assert reported["context"]["configured_tokens"] == reported["context"]["loaded_tokens"] == 4096
                    usage = (await client.get("/v1/usage", headers=auth)).json()
                    assert usage["limits"]["context_tokens"] == 8192 and usage["usage"]["reserved_tokens"] == 0
                    print(json.dumps({"scope": "source_gateway_temporary_database", "native_context_confirmed": 4096, "unlimited_key_respects_model": True, "real_compaction_for_physical_limit": True, "key_policy_preserved": True, "passed": True}, indent=2))
        finally:
            if isinstance(previous, int) and previous > 0:
                restored = await ollama.post("/api/chat", json={"model": model, "messages": [{"role": "user", "content": "OK"}], "stream": False, "think": False, "options": {"num_ctx": previous, "num_predict": 4}})
                restored.raise_for_status()


if __name__ == "__main__":
    asyncio.run(verify())
