"""Text-only Gemini generateContent facade over the existing gateway route."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import StreamingResponse

from app.schemas import ChatCompletionRequest


class TextPart(BaseModel):
    text: str
    model_config = ConfigDict(extra="forbid")


class NativeContent(BaseModel):
    role: Literal["user", "model"] = "user"
    parts: list[TextPart] = Field(min_length=1)
    model_config = ConfigDict(extra="forbid")


class SystemInstruction(BaseModel):
    parts: list[TextPart] = Field(min_length=1)
    model_config = ConfigDict(extra="forbid")


class GenerationConfig(BaseModel):
    max_output_tokens: int | None = Field(default=None, alias="maxOutputTokens", gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, alias="topP", gt=0, le=1)
    model_config = ConfigDict(extra="forbid")


class GeminiRequest(BaseModel):
    contents: list[NativeContent] = Field(min_length=1)
    system_instruction: SystemInstruction | None = Field(default=None, alias="systemInstruction")
    generation_config: GenerationConfig | None = Field(default=None, alias="generationConfig")
    model_config = ConfigDict(extra="forbid")


def to_chat_request(payload: GeminiRequest, model: str, *, stream: bool) -> ChatCompletionRequest:
    messages: list[dict[str, str]] = []
    if payload.system_instruction:
        messages.append({"role": "system", "content": "\n".join(part.text for part in payload.system_instruction.parts)})
    for content in payload.contents:
        messages.append({"role": "assistant" if content.role == "model" else "user", "content": "\n".join(part.text for part in content.parts)})
    config = payload.generation_config
    return ChatCompletionRequest.model_validate({
        "model": model,
        "messages": messages,
        "stream": stream,
        "stream_options": {"include_usage": True} if stream else None,
        "max_completion_tokens": config.max_output_tokens if config else None,
        "temperature": config.temperature if config else None,
        "top_p": config.top_p if config else None,
    })


def _candidate(text: str, *, finish_reason: str | None = None) -> dict[str, Any]:
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": [{"text": text}]}, "index": 0}
    if finish_reason is not None:
        candidate["finishReason"] = finish_reason
    return candidate


def from_chat_response(chat: dict[str, Any], response_id: str) -> dict[str, Any]:
    choice = chat["choices"][0]
    usage = chat["usage"]
    return {
        "responseId": response_id,
        "modelVersion": chat["model"],
        "candidates": [_candidate(choice["message"]["content"] or "", finish_reason="MAX_TOKENS" if choice["finish_reason"] == "length" else "STOP")],
        "usageMetadata": {"promptTokenCount": usage["prompt_tokens"], "candidatesTokenCount": usage["completion_tokens"], "totalTokenCount": usage["total_tokens"]},
    }


async def stream_chat_response(
    chat_stream: StreamingResponse, response_id: str, model: str
) -> AsyncIterator[str]:
    usage: dict[str, int] | None = None
    finish_reason = "STOP"
    async for raw in chat_stream.body_iterator:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        for line in raw.splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            if "error" in chunk:
                yield f"data: {json.dumps({'error': chunk['error']}, ensure_ascii=False)}\n\n"
                return
            if not chunk.get("choices"):
                source = chunk.get("usage") or {}
                usage = {"promptTokenCount": source.get("prompt_tokens", 0), "candidatesTokenCount": source.get("completion_tokens", 0), "totalTokenCount": source.get("total_tokens", 0)}
                continue
            choice = chunk["choices"][0]
            delta = (choice.get("delta") or {}).get("content")
            if isinstance(delta, str) and delta:
                body = {"responseId": response_id, "modelVersion": model, "candidates": [_candidate(delta)]}
                yield f"data: {json.dumps(body, ensure_ascii=False)}\n\n"
            if choice.get("finish_reason") == "length":
                finish_reason = "MAX_TOKENS"
    final: dict[str, Any] = {"responseId": response_id, "modelVersion": model, "candidates": [_candidate("", finish_reason=finish_reason)]}
    if usage is not None:
        final["usageMetadata"] = usage
    yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
