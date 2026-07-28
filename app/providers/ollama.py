from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import settings
from app.errors import GatewayError
from app.schemas import ChatCompletionRequest


class OllamaProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        think: bool | None = None,
    ) -> None:
        self._client = client
        self._think = settings.ollama_think if think is None else think

    def _payload(
        self,
        request: ChatCompletionRequest,
        resolved_model: str,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.top_p is not None:
            options["top_p"] = request.top_p
        if request.output_token_limit is not None:
            options["num_predict"] = request.output_token_limit
        if request.seed is not None:
            options["seed"] = request.seed
        if request.stop is not None:
            options["stop"] = (
                [request.stop] if isinstance(request.stop, str) else request.stop
            )

        messages = [
            {
                "role": (
                    "system" if message.role == "developer" else message.role
                ),
                "content": message.text_content(),
            }
            for message in request.messages
            if message.role != "tool" or message.text_content()
        ]

        payload: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "stream": stream,
            # I modelli Qwen recenti possono spendere tutto il limite di
            # output nel canale "thinking", che Chat Completions non espone.
            "think": self._think,
            "keep_alive": settings.ollama_keep_alive,
        }
        if options:
            payload["options"] = options
        return payload

    async def complete(
        self,
        request: ChatCompletionRequest,
        resolved_model: str,
    ) -> str:
        try:
            response = await self._client.post(
                "/api/chat",
                json=self._payload(request, resolved_model, stream=False),
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise self._provider_error(exc) from exc

        if payload.get("error"):
            raise self._provider_error(RuntimeError(str(payload["error"])))

        content = payload.get("message", {}).get("content", "")
        if not isinstance(content, str):
            raise self._provider_error(RuntimeError("Invalid Ollama response."))
        return content

    async def stream(
        self,
        request: ChatCompletionRequest,
        resolved_model: str,
    ) -> AsyncIterator[str]:
        try:
            async with self._client.stream(
                "POST",
                "/api/chat",
                json=self._payload(request, resolved_model, stream=True),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise RuntimeError(str(event["error"]))
                    content = event.get("message", {}).get("content")
                    if isinstance(content, str) and content:
                        yield content
                    if event.get("done"):
                        break
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            raise self._provider_error(exc) from exc

    @staticmethod
    def _provider_error(exc: Exception) -> GatewayError:
        return GatewayError(
            502,
            f"Local Ollama provider failed: {exc}",
            error_type="provider_error",
            code="ollama_error",
        )
