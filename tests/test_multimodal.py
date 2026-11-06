import base64
import json
from functools import partial

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import settings
from app.database import create_gateway_api
from app.errors import GatewayError
from app.main import app
from app.provider_broker import (
    ClaudeBrokerClient,
    CodexBrokerClient,
    ProviderBrokerError,
)
from app.providers.ollama import OllamaProvider
from app.schemas import ChatCompletionRequest
from app.token_counter import count_message_tokens

JPEG_BYTES = b"\xff\xd8\xff\xe0omni-proxy-test\xff\xd9"
JPEG_BASE64 = base64.b64encode(JPEG_BYTES).decode("ascii")
JPEG_DATA_URL = f"data:image/jpeg;base64,{JPEG_BASE64}"


def test_codex_client_tools_do_not_silently_discard_images_or_charge_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "combination.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", "sk-local-combination-test-key-with-thirty-two-characters")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        profile, key = client.portal.call(partial(create_gateway_api, name="Image tools", provider="codex", model="fixture", reasoning_effort="low", limits={"token_budget": 1000, "budget_period": "daily"}))
        headers = {"Authorization": "Bearer " + key}
        response = client.post("/v1/chat/completions", headers=headers, json={"model": profile.slug, "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": JPEG_DATA_URL}}]}], "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]})
        assert response.status_code == 400 and response.json()["error"]["code"] == "client_tools_images_not_supported"
        usage = client.get("/v1/usage", headers=headers).json()["usage"]
        assert usage["used_tokens"] == usage["reserved_tokens"] == 0


def multimodal_request() -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {
            "model": "vision-test",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": JPEG_DATA_URL},
                        },
                        {"type": "text", "text": "Descrivi l'immagine"},
                    ],
                }
            ],
        }
    )


def test_ollama_payload_translates_image_url_to_native_images():
    request = multimodal_request()
    provider = OllamaProvider(httpx.AsyncClient(), think=False)

    payload = provider._payload(request, "qnick-1:latest", stream=False)

    assert payload["messages"] == [
        {
            "role": "user",
            "content": "Descrivi l'immagine",
            "images": [JPEG_BASE64],
        }
    ]
    assert JPEG_DATA_URL not in str(payload)


@pytest.mark.asyncio
async def test_codex_broker_receives_openai_multimodal_content():
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"content": "pallina arancione visibile"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://codex-broker:8788",
    ) as client:
        result = await CodexBrokerClient(client).complete(
            multimodal_request(),
            model="gpt-5.6-sol",
            reasoning_effort="high",
        )

    assert result == "pallina arancione visibile"
    assert observed["messages"][0]["content"][0]["image_url"]["url"] == (
        JPEG_DATA_URL
    )


@pytest.mark.asyncio
async def test_broker_without_image_transport_rejects_multimodal_request():
    async with httpx.AsyncClient(base_url="http://unused.invalid") as client:
        with pytest.raises(ProviderBrokerError) as error:
            await ClaudeBrokerClient(client).complete(
                multimodal_request(),
                model="claude-test",
                reasoning_effort="auto",
            )

    assert error.value.status_code == 400
    assert error.value.code == "multimodal_not_supported"


def test_remote_image_url_is_rejected_without_network_access():
    with pytest.raises(ValidationError, match="data:image"):
        ChatCompletionRequest.model_validate(
            {
                "model": "vision-test",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "http://127.0.0.1/private.jpg"
                                },
                            }
                        ],
                    }
                ],
            }
        )


def test_image_base64_is_not_counted_as_prompt_text():
    request = multimodal_request()

    assert count_message_tokens(request.messages) < 30


@pytest.mark.asyncio
async def test_ollama_rejects_image_for_model_without_vision_before_inference():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"capabilities": ["completion"]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ollama.test"
    ) as client:
        provider = OllamaProvider(client, think=False)
        with pytest.raises(GatewayError) as error:
            await provider.complete(multimodal_request(), "text-only:latest")

    assert error.value.status_code == 400
    assert error.value.code == "image_input_not_supported"
    assert paths == ["/api/show"]


@pytest.mark.asyncio
async def test_ollama_accepts_image_when_model_declares_vision():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "vision"]})
        return httpx.Response(200, json={"message": {"content": "immagine ricevuta"}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ollama.test"
    ) as client:
        result = await OllamaProvider(client, think=False).complete(
            multimodal_request(), "vision:latest"
        )

    assert result == "immagine ricevuta"
    assert paths == ["/api/show", "/api/chat"]


@pytest.mark.parametrize(
    "extra",
    [
        {"response_format": {"type": "json_object"}},
        {"modalities": ["text", "audio"]},
        {"audio": {"voice": "alloy"}},
    ],
)
def test_unsupported_output_capabilities_are_rejected(extra):
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(
            {
                "model": "local",
                "messages": [{"role": "user", "content": "Ciao"}],
                **extra,
            }
        )


def test_unsupported_message_part_is_rejected_instead_of_dropped():
    with pytest.raises(ValidationError, match="Tipo di contenuto non supportato"):
        ChatCompletionRequest.model_validate(
            {
                "model": "local",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "input_audio", "input_audio": {"data": "AA=="}}],
                    }
                ],
            }
        )
