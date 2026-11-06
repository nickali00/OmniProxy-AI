import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_inference_limits import configure, profile

from app.config import settings
from app.main import _ollama_catalog, app
from app.providers.ollama import OllamaProvider
from app.schemas import ChatCompletionRequest
from app.token_counter import count_request_tokens


def test_physical_context_still_applies_with_unlimited_key_and_can_compact(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "ollama_context_tokens", 512)
    calls = []

    async def complete(self, payload, model):
        calls.append(payload)
        assert count_request_tokens(payload) + payload.output_token_limit <= 512
        return "Retain BLUE." if "Summarize the supplied" in payload.messages[0].text_content() else "BLUE"

    monkeypatch.setattr(OllamaProvider, "complete", complete)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        record, auth = profile(client, {})
        body = {"model": record["slug"], "messages": [{"role": "user", "content": "historical information " * 400}, {"role": "assistant", "content": "BLUE"}, {"role": "user", "content": "Current color?"}], "max_completion_tokens": 64}
        rejected = client.post("/v1/chat/completions", headers=auth, json=body)
        assert rejected.status_code == 400 and rejected.json()["error"]["code"] == "model_context_limit_exceeded" and not calls
        record, auth = profile_with_name(client, {"context_tokens": 8192, "output_tokens": 64, "context_mode": "summarize"})
        body["model"] = record["slug"]
        compacted = client.post("/v1/chat/completions", headers=auth, json=body)
        assert compacted.status_code == 200, compacted.text
        assert compacted.headers["X-OmniProxy-Model-Context"] == "512"
        assert int(compacted.headers["X-OmniProxy-Context-Compactions"]) > 0
        assert client.get("/v1/usage", headers=auth).json()["limits"]["context_tokens"] == 8192


def profile_with_name(client, limits):
    from test_inference_limits import ADMIN
    response = client.post("/api/gateway-apis", headers=ADMIN, json={"name": "Physical limits", "provider": "ollama", "model": "fixture", "reasoning_effort": "off", "limits": limits})
    assert response.status_code == 201
    return response.json(), {"Authorization": f"Bearer {response.json()['api_key']}"}


@pytest.mark.asyncio
async def test_native_request_sets_context_and_catalog_distinguishes_loaded_reported(monkeypatch):
    monkeypatch.setattr(settings, "ollama_context_tokens", 8192)
    seen = []

    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "fixture", "context_length": 4096}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "vision", "tools"], "model_info": {"general.architecture": "fixture", "fixture.context_length": 32768}})
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "OK"}})

    async with httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(handler)) as client:
        async def discovered(*_args, **_kwargs):
            return client, {"models": [{"name": "fixture"}]}
        monkeypatch.setattr("app.main._discover_ollama", discovered)
        catalog = await _ollama_catalog(app)
        model = catalog["models"][0]
        assert model["context"] == {"reported_tokens": 32768, "configured_tokens": 8192, "loaded_tokens": 4096, "counting": "gateway_estimate"}
        assert model["gateway_capabilities"]["client_tools"] is False
        assert model["reported_capabilities"] == ["completion", "vision", "tools"]
        result = await OllamaProvider(client).complete(ChatCompletionRequest(model="local", messages=[{"role": "user", "content": "hello"}]), "fixture")
        assert result == "OK" and seen[0]["options"]["num_ctx"] == 8192
