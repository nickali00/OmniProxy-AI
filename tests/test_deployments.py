import asyncio
import json

import httpx
import pytest

from app import deployments
from app.config import settings
from app.errors import GatewayError
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.routing import public_models, resolve_model_route
from app.schemas import ChatCompletionRequest


def configure(monkeypatch, **primary):
    targets = [{"id": "first", "base_url": "http://first/v1", "model": "model-a", "api_key": "private-a", "max_concurrency": 1, "fallbacks": ["second"], "fallback_on": ["busy", "connect_error", "quota"], **primary}, {"id": "second", "base_url": "http://second/v1", "model": "model-b", "api_key": "private-b"}]
    monkeypatch.setattr(settings, "upstream_deployments_json", json.dumps(targets))
    monkeypatch.setattr(settings, "upstream_base_url", "")
    monkeypatch.setattr(settings, "upstream_model", "")


def request(**extra):
    return ChatCompletionRequest(model="upstream:first", messages=[{"role": "user", "content": "hello"}], **extra)


def answer(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.mark.asyncio
async def test_catalog_aliases_and_separate_credentials(monkeypatch):
    configure(monkeypatch)
    seen = []

    def handler(req):
        host = req.url.host
        assert req.headers["authorization"] == ("Bearer private-a" if host == "first" else "Bearer private-b")
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "model-a" if host == "first" else "model-b"}]})
        seen.append(host)
        assert json.loads(req.content)["model"] == ("model-a" if host == "first" else "model-b")
        return answer(host)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleProvider(client)
        catalog = await adapter.catalog()
        assert {item["id"] for item in catalog["models"]} == {"upstream:first", "upstream:second"}
        assert "private" not in json.dumps(catalog) and "http://" not in json.dumps(catalog)
        assert await adapter.complete(request(), "upstream:first") == "first"
        assert await adapter.complete(request(), "upstream:second") == "second"
    assert seen == ["first", "second"]
    assert resolve_model_route("upstream:first").resolved_model == "upstream:first"
    assert "upstream:second" in {item["id"] for item in public_models()}


@pytest.mark.asyncio
async def test_busy_fallback_and_cancel_releases_capacity(monkeypatch):
    configure(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def handler(req):
        seen.append(req.url.host)
        if req.url.host == "first":
            started.set()
            await release.wait()
        return answer(req.url.host)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleProvider(client)
        first = asyncio.create_task(adapter.complete(request(), "upstream:first"))
        await asyncio.wait_for(started.wait(), 2)
        assert await adapter.complete(request(), "upstream:first") == "second"
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert deployments.diagnostics(deployments.configured()["first"])["active"] == 0
        release.set()
        assert await adapter.complete(request(), "upstream:first") == "first"
    assert seen == ["first", "second", "first"]


@pytest.mark.asyncio
async def test_quota_cooldown_and_client_tools_are_never_retried(monkeypatch):
    configure(monkeypatch)
    seen = []

    def handler(req):
        seen.append(req.url.host)
        return httpx.Response(429, headers={"Retry-After": "20"}, text="private quota details") if req.url.host == "first" else answer("second")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleProvider(client)
        assert await adapter.complete(request(), "upstream:first") == "second"
        assert await adapter.complete(request(), "upstream:first") == "second"
        capacity, _ = deployments.state_for(deployments.configured()["first"])
        capacity.blocked_until = 0
        tools = request(tools=[{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}])
        with pytest.raises(GatewayError) as error:
            await adapter.complete_with_tools(tools, "upstream:first")
        assert error.value.code == "upstream_quota_exceeded" and error.value.headers["Retry-After"] == "20"
        assert "private" not in error.value.message
    assert seen == ["first", "second", "second", "first"]


class BrokenStream(httpx.AsyncByteStream):
    def __init__(self, emitted):
        self.emitted = emitted

    async def __aiter__(self):
        if self.emitted:
            yield b'data: {"choices":[{"delta":{"content":"first chunk"}}]}\n\n'
        raise httpx.ConnectError("fixture")


@pytest.mark.asyncio
@pytest.mark.parametrize("emitted", [False, True])
async def test_stream_fallback_stops_after_first_output(monkeypatch, emitted):
    configure(monkeypatch)
    seen = []

    def handler(req):
        seen.append(req.url.host)
        if req.url.host == "first":
            return httpx.Response(200, stream=BrokenStream(emitted))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"fallback"}}]}\n\ndata: [DONE]\n\n')

    chunks = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        try:
            async for chunk in OpenAICompatibleProvider(client).stream(request(), "upstream:first"):
                chunks.append(chunk)
        except GatewayError:
            assert emitted
    assert chunks == (["first chunk"] if emitted else ["fallback"])
    assert seen == (["first"] if emitted else ["first", "second"])
    assert deployments.diagnostics(deployments.configured()["first"])["active"] == 0


@pytest.mark.asyncio
async def test_shared_account_capacity_and_disabled_fallback(monkeypatch):
    configure(monkeypatch, capacity_group="shared", fallback_on=[])
    values = json.loads(settings.upstream_deployments_json)
    values[1]["capacity_group"] = "shared"
    monkeypatch.setattr(settings, "upstream_deployments_json", json.dumps(values))
    started, release = asyncio.Event(), asyncio.Event()

    async def handler(_req):
        started.set()
        await release.wait()
        return answer("ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleProvider(client)
        first = asyncio.create_task(adapter.complete(request(), "upstream:first"))
        await started.wait()
        try:
            with pytest.raises(GatewayError) as error:
                await adapter.complete(request(), "upstream:second")
            assert error.value.code == "deployment_busy"
        finally:
            release.set()
            await first


@pytest.mark.parametrize("raw", ['{"api_key":"private"}', '[{"id":"x","base_url":"http://u:private@host/v1","model":"x"}]', '[{"id":"x","base_url":"http://host/v1","model":"x","fallbacks":["missing"]}]'])
def test_bad_server_config_is_sanitized(monkeypatch, raw):
    monkeypatch.setattr(settings, "upstream_deployments_json", raw)
    with pytest.raises(GatewayError) as error:
        deployments.configured()
    assert error.value.code == "invalid_deployment_configuration" and "private" not in error.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_total_deadline_does_not_retry_and_releases_slot(monkeypatch, streaming):
    configure(monkeypatch)
    monkeypatch.setattr(settings, "provider_completion_timeout_seconds", 0.03)
    seen = []

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
            await asyncio.sleep(10)

    async def handler(req):
        seen.append(req.url.host)
        if streaming:
            return httpx.Response(200, stream=SlowStream())
        await asyncio.sleep(10)
        return answer("late")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleProvider(client)
        with pytest.raises(GatewayError) as error:
            if streaming:
                async for _ in adapter.stream(request(), "upstream:first"):
                    pass
            else:
                await adapter.complete(request(), "upstream:first")
        assert error.value.code == "upstream_timeout"
    assert seen == ["first"]
    assert deployments.diagnostics(deployments.configured()["first"])["active"] == 0
