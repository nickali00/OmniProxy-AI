import base64
import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import image_api
from app.config import settings
from app.database import _connect
from app.errors import GatewayError
from app.main import app

KEY = "sk-local-image-test-key-with-thirty-two-characters"
HEADERS = {"Authorization": f"Bearer {KEY}"}


def png(width=256, height=256):
    output = io.BytesIO()
    Image.new("RGB", (width, height), "blue").save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "image.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)
    monkeypatch.setattr(settings, "image_backend_url", "http://fixture/v1")
    monkeypatch.setattr(settings, "image_local_model", "")
    async def remote(payload):
        width, height = map(int, payload.size.split("x"))
        return [png(width, height) for _ in range(payload.n)]
    monkeypatch.setattr(image_api, "remote", remote)
    with TestClient(app, base_url="http://127.0.0.1") as instance:
        yield instance


async def policy(limits):
    async with _connect() as db:
        await db.execute("UPDATE api_keys SET limits_json=?", (json.dumps(limits),))
        await db.commit()


def test_image_normalization_budget_and_insufficient_complete_image(client):
    client.portal.call(policy, {"token_budget": 1000, "budget_period": "daily"})
    response = client.post("/v1/images/generations", headers=HEADERS, json={"prompt": "A blue square.", "size": "256x256"})
    assert response.status_code == 200, response.text
    with Image.open(io.BytesIO(base64.b64decode(response.json()["data"][0]["b64_json"]))) as image:
        assert image.size == (256, 256) and image.format == "PNG"
    usage = client.get("/v1/usage", headers=HEADERS).json()["usage"]
    assert usage["used_tokens"] == response.json()["usage"]["total_tokens"] and usage["reserved_tokens"] == 0
    rejected = client.post("/v1/images/generations", headers=HEADERS, json={"prompt": "A blue square.", "size": "256x256", "n": 3})
    assert rejected.status_code == 429 and rejected.json()["error"]["code"] == "image_budget_insufficient"
    after = client.get("/v1/usage", headers=HEADERS).json()["usage"]
    assert after["used_tokens"] == usage["used_tokens"] and after["reserved_tokens"] == 0


def test_key_input_output_context_and_pixels_fail_before_generation(client, monkeypatch):
    async def forbidden(_):
        pytest.fail("Backend must not be called")
    monkeypatch.setattr(image_api, "remote", forbidden)
    for limits, prompt, code in [({"input_tokens": 1}, "More than one token", "input_token_limit_exceeded"), ({"output_tokens": 100}, "blue", "output_token_limit_exceeded"), ({"context_tokens": 512}, "word " * 500, "context_limit_exceeded")]:
        client.portal.call(policy, limits)
        response = client.post("/v1/images/generations", headers=HEADERS, json={"prompt": prompt, "size": "256x256"})
        assert response.status_code == 400 and response.json()["error"]["code"] == code
    too_big = client.post("/v1/images/generations", headers=HEADERS, json={"prompt": "blue", "size": "2048x2048"})
    assert too_big.status_code == 400 and too_big.json()["error"]["code"] == "image_size_not_supported"


def test_wrong_dimensions_and_missing_backend_are_explicit(client, monkeypatch):
    async def wrong(_):
        return [png(8, 8)]
    monkeypatch.setattr(image_api, "remote", wrong)
    result = client.post("/v1/images/generations", headers=HEADERS, json={"prompt": "blue", "size": "256x256"})
    assert result.status_code == 502 and result.json()["error"]["code"] == "image_generation_failed"
    assert client.get("/v1/usage", headers=HEADERS).json()["usage"]["reserved_tokens"] == 0
    monkeypatch.setattr(settings, "image_backend_url", "")
    unavailable = client.post("/v1/images/generations", headers=HEADERS, json={"prompt": "blue"})
    assert unavailable.status_code == 503 and unavailable.json()["error"]["code"] == "image_provider_not_configured"


@pytest.mark.asyncio
async def test_upstream_auth_is_server_owned_and_remote_urls_are_never_fetched(monkeypatch):
    monkeypatch.setattr(settings, "image_backend_url", "http://image-host/v1")
    monkeypatch.setattr(settings, "image_api_key", "private-key")
    monkeypatch.setattr(settings, "image_model", "configured-model")
    original = httpx.AsyncClient
    seen = []

    def handler(request):
        seen.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer private-key"
        assert json.loads(request.content)["model"] == "configured-model"
        return httpx.Response(200, json={"data": [{"url": "http://169.254.169.254/private"}]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(GatewayError) as error:
        await image_api.remote(image_api.ImageRequest(prompt="blue"))
    assert error.value.code == "image_upstream_error" and "private" not in error.value.message
    assert seen == ["http://image-host/v1/images/generations"]
