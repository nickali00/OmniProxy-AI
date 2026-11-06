"""Text-only Anthropic Messages facade over the existing gateway route."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import StreamingResponse

from app.errors import GatewayError
from app.schemas import ChatCompletionRequest


class NativeMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]]
    model_config = ConfigDict(extra="forbid")


class AnthropicRequest(BaseModel):
    model: str = Field(min_length=1)
    max_tokens: int = Field(gt=0)
    messages: list[NativeMessage] = Field(min_length=1)
    system: str | None = None
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=1)
    top_p: float | None = Field(default=None, gt=0, le=1)
    model_config = ConfigDict(extra="forbid")


def to_chat_request(payload: AnthropicRequest) -> ChatCompletionRequest:
    messages: list[dict[str, Any]] = []
    if payload.system:
        messages.append({"role": "system", "content": payload.system})
    for message in payload.messages:
        if isinstance(message.content, str):
            content = message.content
        else:
            parts = message.content
            if any(
                not isinstance(part, dict)
                or part.get("type") != "text"
                or not isinstance(part.get("text"), str)
                for part in parts
            ):
                raise GatewayError(400, "Sono supportati solo blocchi testo Messages.", code="native_content_not_supported")
            content = "\n".join(part["text"] for part in parts)
        messages.append({"role": message.role, "content": content})
    return ChatCompletionRequest.model_validate({
        "model": payload.model,
        "messages": messages,
        "max_completion_tokens": payload.max_tokens,
        "stream": payload.stream,
        "stream_options": {"include_usage": True} if payload.stream else None,
        "temperature": payload.temperature,
        "top_p": payload.top_p,
    })


def from_chat_response(chat: dict[str, Any], message_id: str) -> dict[str, Any]:
    choice = chat["choices"][0]
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": chat["model"],
        "content": [{"type": "text", "text": choice["message"]["content"] or ""}],
        "stop_reason": "max_tokens" if choice["finish_reason"] == "length" else "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": chat["usage"]["prompt_tokens"],
            "output_tokens": chat["usage"]["completion_tokens"],
        },
    }


async def stream_chat_response(
    chat_stream: StreamingResponse, message_id: str, model: str
) -> AsyncIterator[str]:
    def event(name: str, body: dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"

    yield event("message_start", {"type": "message_start", "message": {
        "id": message_id, "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }})
    yield event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
    stop_reason = "end_turn"
    async for raw in chat_stream.body_iterator:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        for line in raw.splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            if "error" in chunk:
                yield event("error", {"type": "error", "error": chunk["error"]})
                return
            if not chunk.get("choices"):
                source = chunk.get("usage") or {}
                usage = {
                    "input_tokens": source.get("prompt_tokens", 0),
                    "output_tokens": source.get("completion_tokens", 0),
                }
                continue
            choice = chunk["choices"][0]
            content = (choice.get("delta") or {}).get("content")
            if isinstance(content, str) and content:
                yield event("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": content}})
            if choice.get("finish_reason") == "length":
                stop_reason = "max_tokens"
    yield event("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield event("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": usage})
    yield event("message_stop", {"type": "message_stop"})
