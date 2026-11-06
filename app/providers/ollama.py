from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import settings
from app.errors import GatewayError
from app.schemas import ChatCompletionRequest
from app.token_counter import count_request_tokens


class OllamaProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        think: bool | None = None,
    ) -> None:
        self._client = client
        self._think = settings.ollama_think if think is None else think

    async def _require_model_capability(
        self,
        request: ChatCompletionRequest,
        resolved_model: str,
    ) -> None:
        try:
            response = await self._client.post(
                "/api/show", json={"model": resolved_model}, timeout=4.0
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise self._provider_error(exc) from exc
        capabilities = payload.get("capabilities") if isinstance(payload, dict) else None
        if request.has_images and (not isinstance(capabilities, list) or "vision" not in capabilities):
            raise GatewayError(
                400,
                "Il modello Ollama selezionato non dichiara la capacità vision.",
                error_type="invalid_request_error",
                code="image_input_not_supported",
                param="messages",
            )
        info = payload.get("model_info", {}) if isinstance(payload, dict) else {}
        if isinstance(info, dict):
            trained = info.get(str(info.get("general.architecture")) + ".context_length")
            if isinstance(trained, int) and not isinstance(trained, bool) and trained > 0:
                context = min(settings.ollama_context_tokens, trained)
                if count_request_tokens(request) + (request.output_token_limit or 0) > context:
                    raise GatewayError(400, "La richiesta supera il contesto dichiarato dal modello locale.", code="model_context_limit_exceeded", param="messages", headers={"X-OmniProxy-Model-Context": str(context)})

    def _payload(
        self,
        request: ChatCompletionRequest,
        resolved_model: str,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        if count_request_tokens(request) + (request.output_token_limit or 0) > settings.ollama_context_tokens:
            raise GatewayError(400, "La richiesta supera il contesto locale configurato per il modello.", code="model_context_limit_exceeded", param="messages")
        options: dict[str, Any] = {"num_ctx": settings.ollama_context_tokens}
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

        messages: list[dict[str, Any]] = []
        for message in request.messages:
            text = message.text_content()
            images = message.image_payloads()
            if message.role == "tool" and not text and not images:
                continue
            ollama_message: dict[str, Any] = {
                "role": (
                    "system" if message.role == "developer" else message.role
                ),
                "content": text,
            }
            if images:
                ollama_message["images"] = [
                    image.base64_data for image in images
                ]
            messages.append(ollama_message)

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
        await self._require_model_capability(request, resolved_model)
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
        await self._require_model_capability(request, resolved_model)
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
            "Local Ollama provider failed.",
            error_type="provider_error",
            code="ollama_error",
        )
