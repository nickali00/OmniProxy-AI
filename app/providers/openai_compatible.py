"""A server-configured, text-only OpenAI-compatible upstream adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.errors import GatewayError
from app.network_security import enforce_upstream_transport
from app.schemas import ChatCompletionRequest


def upstream_chat_url(base_url: str | None = None) -> str:
    configured = (settings.upstream_base_url if base_url is None else base_url).strip().rstrip("/")
    parts = urlsplit(configured)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise GatewayError(503, "Upstream OpenAI compatibile non configurato.", code="upstream_not_configured")
    return f"{configured}/chat/completions"


class OpenAICompatibleProvider:
    def __init__(self, client: httpx.AsyncClient | None = None, *, deployment=None) -> None:
        self._client = client
        self._deployment = deployment

    def _url(self):
        return upstream_chat_url(self._deployment.base_url if self._deployment else None)

    async def catalog(self) -> dict[str, Any]:
        from app import deployments

        result: dict[str, Any] = {"id": "openai_compatible", "name": "OpenAI compatible", "connected": False, "models": [], "message": "Upstream non configurato o non raggiungibile."}
        configured = deployments.configured() if self._deployment is None else {}
        if configured:
            children = await asyncio.gather(*(OpenAICompatibleProvider(self._client, deployment=target).catalog() for target in configured.values()))
            observations = await asyncio.gather(*(deployments.observe(target) for target in configured.values()))
            capacity_by_id = {item["id"]: item for item in observations}
            for target, child in zip(configured.values(), children, strict=True):
                result["models"].extend({**model, "id": "upstream:" + target.id, "display_name": target.id + " · " + target.model, "is_default": False, "deployment": capacity_by_id[target.id]} for model in child["models"])
            result["connected"] = bool(result["models"])
            if result["connected"]:
                result["message"] = "Cataloghi deployment verificati."
            result["deployments"] = observations
        model = self._deployment.model if self._deployment else settings.upstream_model
        if not (self._deployment or settings.upstream_base_url) or not model:
            return result
        try:
            url = self._url().rsplit("/", 1)[0] + "/models"
            await enforce_upstream_transport(url)
            if self._client is None:
                async with httpx.AsyncClient(timeout=3) as client:
                    response = await client.get(url, headers=self._headers())
            else:
                response = await self._client.get(url, headers=self._headers(), timeout=3)
            response.raise_for_status()
            models = response.json()["data"]
            if not isinstance(models, list) or not any(isinstance(item, dict) and item.get("id") == model for item in models):
                return result
        except (httpx.HTTPError, ValueError, KeyError, TypeError, GatewayError):
            return result
        result.update(connected=True, message="Catalogo upstream verificato.")
        result["models"].append({
            "id": model, "display_name": model,
            "description": "OpenAI-compatible upstream", "is_default": True,
            "reasoning_efforts": ["auto"], "default_reasoning_effort": "auto",
        })
        return result

    def _headers(self) -> dict[str, str]:
        key = self._deployment.api_key.get_secret_value() if self._deployment else settings.upstream_api_key
        return {"Authorization": f"Bearer {key}"} if key else {}

    @staticmethod
    def _payload(request: ChatCompletionRequest, model: str, *, stream: bool, allow_tools: bool = False, allow_images: bool = False) -> dict[str, object]:
        if request.has_images and not allow_images:
            raise GatewayError(400, "Le immagini non sono ancora verificate per questo upstream.", code="image_input_not_supported")
        if request.has_client_tools and not allow_tools:
            raise GatewayError(400, "I tool client non sono ancora verificati per questo upstream.", code="client_tools_not_supported")
        payload: dict[str, object] = {
            "model": model,
            "messages": [
                item.model_dump(exclude_none=True) if allow_tools or request.has_images
                else {"role": item.role, "content": item.text_content()}
                for item in request.messages
            ],
            "stream": stream,
        }
        if allow_tools:
            if request.tools:
                payload["tools"] = request.tools
            if request.tool_choice is not None:
                payload["tool_choice"] = request.tool_choice
            payload["parallel_tool_calls"] = False
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.output_token_limit is not None:
            payload["max_completion_tokens"] = request.output_token_limit
        return payload

    @staticmethod
    def _error(exc=None) -> GatewayError:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
            try:
                retry = str(min(600, max(1, int(exc.response.headers.get("Retry-After", "60")))))
            except ValueError:
                retry = "60"
            return GatewayError(429, "Quota upstream temporaneamente esaurita.", code="upstream_quota_exceeded", headers={"Retry-After": retry})
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            return GatewayError(502, "Deployment upstream non raggiungibile.", code="upstream_unreachable")
        return GatewayError(502, "L'upstream OpenAI compatibile non ha restituito una risposta valida.", error_type="provider_error", code="upstream_error")

    async def complete(self, request: ChatCompletionRequest, resolved_model: str) -> str:
        if self._deployment is None and resolved_model.startswith("upstream:"):
            from app import deployments
            return await deployments.complete(self._client, request, resolved_model)
        url = self._url()
        await enforce_upstream_transport(url)
        payload = self._payload(request, resolved_model, stream=False, allow_images=bool(self._deployment and self._deployment.images))
        try:
            if self._client is None:
                async with httpx.AsyncClient(timeout=settings.provider_completion_timeout_seconds) as client:
                    response = await client.post(url, json=payload, headers=self._headers())
            else:
                response = await self._client.post(url, json=payload, headers=self._headers())
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("Invalid content")
            return content
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise self._error(exc) from exc

    async def complete_with_tools(
        self, request: ChatCompletionRequest, resolved_model: str
    ) -> dict[str, Any]:
        if self._deployment is None and resolved_model.startswith("upstream:"):
            from app import deployments
            return await deployments.complete(self._client, request, resolved_model, tools=True)
        url = self._url()
        await enforce_upstream_transport(url)
        payload = self._payload(request, resolved_model, stream=False, allow_tools=True, allow_images=bool(self._deployment and self._deployment.images))
        try:
            if self._client is None:
                async with httpx.AsyncClient(timeout=settings.provider_completion_timeout_seconds) as client:
                    response = await client.post(url, json=payload, headers=self._headers())
            else:
                response = await self._client.post(url, json=payload, headers=self._headers())
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            content = message.get("content")
            calls = message.get("tool_calls") or []
            if not isinstance(content, (str, type(None))) or not isinstance(calls, list):
                raise TypeError("Invalid tool response")
            for call in calls:
                if (
                    not isinstance(call, dict)
                    or not isinstance(call.get("id"), str)
                    or call.get("type") != "function"
                    or not isinstance(call.get("function"), dict)
                    or not isinstance(call["function"].get("name"), str)
                    or not isinstance(call["function"].get("arguments"), str)
                ):
                    raise ValueError("Invalid function call")
            return {"content": content, "tool_calls": calls}
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise self._error(exc) from exc

    async def stream(self, request: ChatCompletionRequest, resolved_model: str) -> AsyncIterator[str]:
        if self._deployment is None and resolved_model.startswith("upstream:"):
            from app import deployments
            async for chunk in deployments.stream(self._client, request, resolved_model):
                yield chunk
            return
        url = self._url()
        await enforce_upstream_transport(url)
        payload = self._payload(request, resolved_model, stream=True, allow_images=bool(self._deployment and self._deployment.images))
        try:
            if self._client is None:
                async with httpx.AsyncClient(timeout=settings.provider_completion_timeout_seconds) as client:
                    async for chunk in self._stream_with_client(client, url, payload):
                        yield chunk
            else:
                async for chunk in self._stream_with_client(self._client, url, payload):
                    yield chunk
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise self._error(exc) from exc

    async def _stream_with_client(
        self, client: httpx.AsyncClient, url: str, payload: dict[str, object]
    ) -> AsyncIterator[str]:
        async with client.stream("POST", url, json=payload, headers=self._headers()) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                if line == "data: [DONE]":
                    return
                event = json.loads(line[6:])
                if "error" in event:
                    raise ValueError("Upstream stream error")
                choices = event.get("choices") or []
                if not choices:
                    continue
                content = choices[0].get("delta", {}).get("content")
                if isinstance(content, str) and content:
                    yield content
