import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.errors import GatewayError
from app.main import app
from app.providers.openai_compatible import OpenAICompatibleProvider, upstream_chat_url
from app.routing import public_models, resolve_model_route
from app.schemas import ChatCompletionRequest


KEY = "sk-local-test-key-with-at-least-thirty-two-characters"


def _request():
    return ChatCompletionRequest.model_validate({"model": "upstream", "messages": [{"role": "user", "content": "Ciao"}]})


@pytest.mark.asyncio
async def test_upstream_adapter_text_and_stream(monkeypatch):
    monkeypatch.setattr(settings, "upstream_base_url", "http://model-host:1234/v1")
    monkeypatch.setattr(settings, "upstream_api_key", "test-secret")
    seen = []

    def handler(request):
        seen.append(request)
        assert str(request.url) == "http://model-host:1234/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-secret"
        body = json.loads(request.content)
        assert body["model"] == "example-model"
        assert body["messages"] == [{"role": "user", "content": "Ciao"}]
        if body["stream"]:
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"uno"}}]}\n\ndata: {"choices":[{"delta":{"content":" due"}}]}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={"choices": [{"message": {"content": "uno due"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleProvider(client)
        assert await adapter.complete(_request(), "example-model") == "uno due"
        assert [chunk async for chunk in adapter.stream(_request(), "example-model")] == ["uno", " due"]
    assert len(seen) == 2


def test_upstream_alias_and_gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)
    monkeypatch.setattr(settings, "upstream_base_url", "http://model-host:1234/v1")
    monkeypatch.setattr(settings, "upstream_model", "example-model")
    monkeypatch.setattr(settings, "upstream_api_key", "")
    assert resolve_model_route("upstream").provider == "openai_compatible"
    assert "upstream" in {model["id"] for model in public_models()}

    async def fake_complete(self, request, model):
        assert model == "example-model"
        return "risposta upstream"

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", fake_complete)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {KEY}"}, json={"model": "upstream", "messages": [{"role": "user", "content": "Ciao"}]})
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "risposta upstream"


def test_upstream_rejects_invalid_url_and_unverified_images(monkeypatch):
    monkeypatch.setattr(settings, "upstream_base_url", "http://user:pass@host/v1")
    with pytest.raises(GatewayError) as error:
        upstream_chat_url()
    assert error.value.code == "upstream_not_configured"
    request = ChatCompletionRequest.model_validate({"model": "upstream", "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="}}]}]})
    with pytest.raises(GatewayError) as error:
        OpenAICompatibleProvider._payload(request, "model", stream=False)
    assert error.value.code == "image_input_not_supported"


@pytest.mark.asyncio
async def test_upstream_client_tool_passthrough(monkeypatch):
    monkeypatch.setattr(settings, "upstream_base_url", "http://model-host:1234/v1")
    monkeypatch.setattr(settings, "upstream_api_key", "")

    def handler(request):
        body = json.loads(request.content)
        assert body["tools"][0]["function"]["name"] == "run_terminal"
        assert body["messages"][-1] == {"role": "tool", "content": "OK", "tool_call_id": "call_old"}
        assert body["parallel_tool_calls"] is False
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [{"id": "call_next", "type": "function", "function": {"name": "run_terminal", "arguments": '{"command":"pwd"}'}}]}}]})

    request = ChatCompletionRequest.model_validate({"model": "upstream", "messages": [
        {"role": "user", "content": "Continua"},
        {"role": "assistant", "tool_calls": [{"id": "call_old", "type": "function", "function": {"name": "run_terminal", "arguments": '{"command":"echo OK"}'}}]},
        {"role": "tool", "tool_call_id": "call_old", "content": "OK"},
    ], "tools": [{"type": "function", "function": {"name": "run_terminal", "parameters": {"type": "object"}}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAICompatibleProvider(client).complete_with_tools(request, "example-model")
    assert result["tool_calls"][0]["id"] == "call_next"


def test_gateway_upstream_tool_response(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)
    monkeypatch.setattr(settings, "upstream_base_url", "http://model-host:1234/v1")
    monkeypatch.setattr(settings, "upstream_model", "example-model")

    async def fake_complete(self, request, model):
        return {"content": None, "tool_calls": [{"id": "call_next", "type": "function", "function": {"name": "run_terminal", "arguments": '{"command":"pwd"}'}}]}

    monkeypatch.setattr(OpenAICompatibleProvider, "complete_with_tools", fake_complete)
    body = {"model": "upstream", "messages": [{"role": "user", "content": "Usa il tool"}], "tools": [{"type": "function", "function": {"name": "run_terminal", "parameters": {"type": "object"}}}]}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {KEY}"}, json=body)
        assert response.status_code == 200
        assert response.json()["choices"][0]["finish_reason"] == "tool_calls"
        assert response.json()["choices"][0]["message"]["tool_calls"][0]["id"] == "call_next"


@pytest.mark.asyncio
async def test_upstream_catalog_only_advertises_reachable_configured_model(monkeypatch):
    monkeypatch.setattr(settings, "upstream_base_url", "http://model-host:1234/v1")
    monkeypatch.setattr(settings, "upstream_model", "test-model")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [{"id": "test-model"}]}))) as client:
        catalog = await OpenAICompatibleProvider(client).catalog()
        assert catalog["connected"] is True
        assert catalog["models"][0]["id"] == "test-model"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(401, text="private upstream error"))) as client:
        catalog = await OpenAICompatibleProvider(client).catalog()
        assert catalog["connected"] is False
        assert "private" not in str(catalog)


def test_upstream_profile_created_from_dashboard_and_routes_bound_model(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)

    async def fake_catalog(_app):
        return [{"id": "openai_compatible", "name": "Upstream", "connected": True, "models": [{"id": "bound-model", "reasoning_efforts": ["auto"]}]}]

    async def fake_complete(self, request, model):
        assert model == "bound-model"
        return "bound response"

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    monkeypatch.setattr(OpenAICompatibleProvider, "complete", fake_complete)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        profile = client.post("/api/gateway-apis", headers={"Origin": "http://127.0.0.1", "X-OmniProxy-Request": "dashboard"}, json={"name": "Upstream profile", "provider": "openai_compatible", "model": "bound-model", "reasoning_effort": "auto"})
        assert profile.status_code == 201
        headers = {"Authorization": f"Bearer {profile.json()['api_key']}"}
        response = client.post("/v1/chat/completions", headers=headers, json={"model": "client-cannot-override", "messages": [{"role": "user", "content": "Hello"}]})
        assert response.status_code == 200
        assert response.json()["model"] == profile.json()["slug"]
        assert response.json()["choices"][0]["message"]["content"] == "bound response"
