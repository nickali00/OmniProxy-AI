"""Small asynchronous client for the OmniProxy AI OpenAI-compatible API."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp


class OmniProxyApiError(Exception):
    """Base exception raised by the OmniProxy API client."""


class OmniProxyAuthenticationError(OmniProxyApiError):
    """The gateway rejected the local API key."""


class OmniProxyConnectionError(OmniProxyApiError):
    """The gateway could not be reached."""


class OmniProxyResponseError(OmniProxyApiError):
    """The gateway returned an unexpected response."""


def normalize_base_url(value: str) -> str:
    """Normalize a gateway URL and ensure it targets the versioned API."""
    raw_value = value.strip()
    parsed = urlsplit(raw_value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid OmniProxy AI URL")

    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid OmniProxy AI port") from exc

    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    elif not path.endswith("/v1"):
        path = f"{path}/v1"

    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def parse_model_ids(payload: Any) -> list[str]:
    """Extract safe model identifiers from an OpenAI-compatible response."""
    if not isinstance(payload, Mapping):
        raise OmniProxyResponseError("Invalid models response")
    raw_models = payload.get("data")
    if not isinstance(raw_models, list):
        raise OmniProxyResponseError("Missing models list")

    models: list[str] = []
    for item in raw_models:
        if not isinstance(item, Mapping):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id.strip():
            models.append(model_id.strip())
    if not models:
        raise OmniProxyResponseError("No models available for this API key")
    return models


class OmniProxyClient:
    """Call an OmniProxy AI instance without exposing its key in logs."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str,
        *,
        request_timeout: int,
    ) -> None:
        self._session = session
        self.base_url = normalize_base_url(base_url)
        self._api_key = api_key.strip()
        self._request_timeout = request_timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def async_models(self, *, timeout: int | None = None) -> list[str]:
        """Return the models visible to the configured local API key."""
        payload = await self._async_json_request(
            "GET",
            f"{self.base_url}/models",
            timeout=timeout,
        )
        return parse_model_ids(payload)

    async def async_chat(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, str]],
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Create a non-streaming chat completion."""
        payload = await self._async_json_request(
            "POST",
            f"{self.base_url}/chat/completions",
            json={
                "model": model,
                "messages": list(messages),
                "max_completion_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            },
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise OmniProxyResponseError(
                "Missing assistant content in the gateway response"
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise OmniProxyResponseError("The gateway returned an empty response")
        return content.strip()

    async def _async_json_request(
        self,
        method: str,
        url: str,
        *,
        timeout: int | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> Any:
        effective_timeout = timeout or self._request_timeout
        try:
            async with asyncio.timeout(effective_timeout):
                async with self._session.request(
                    method,
                    url,
                    headers=self._headers,
                    json=json,
                ) as response:
                    if response.status in {401, 403}:
                        raise OmniProxyAuthenticationError(
                            "OmniProxy AI rejected the local API key"
                        )
                    if response.status >= 400:
                        raise OmniProxyResponseError(
                            f"OmniProxy AI returned HTTP {response.status}"
                        )
                    try:
                        return await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError) as exc:
                        raise OmniProxyResponseError(
                            "OmniProxy AI returned invalid JSON"
                        ) from exc
        except OmniProxyApiError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            raise OmniProxyConnectionError("Unable to connect to OmniProxy AI") from exc
