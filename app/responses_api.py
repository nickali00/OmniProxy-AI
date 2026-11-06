"""A bounded Responses adapter over the existing Chat Completions contract."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import StreamingResponse

from app.errors import GatewayError
from app.schemas import ChatCompletionRequest, ChatMessage


class ResponsesRequest(BaseModel):
    model: str = Field(min_length=1)
    input: str | list[dict[str, Any]]
    instructions: str | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: str | dict[str, Any] = "auto"
    stream: bool = False
    store: bool = False
    previous_response_id: str | None = None
    max_output_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    parallel_tool_calls: bool = False

    model_config = ConfigDict(extra="forbid")


def _unsupported(message: str, *, code: str = "unsupported_response_feature") -> GatewayError:
    return GatewayError(
        400, message, error_type="invalid_request_error", code=code
    )


def to_chat_request(payload: ResponsesRequest) -> ChatCompletionRequest:
    if payload.parallel_tool_calls:
        raise _unsupported("Le chiamate tool parallele non sono supportate.")

    messages: list[dict[str, Any]] = []
    if payload.instructions:
        messages.append({"role": "developer", "content": payload.instructions})
    if isinstance(payload.input, str):
        messages.append({"role": "user", "content": payload.input})
    else:
        for item in payload.input:
            item_type = item.get("type", "message")
            if item_type == "message":
                role = item.get("role")
                content = item.get("content")
                if role not in {"system", "developer", "user", "assistant"}:
                    raise _unsupported("Ruolo Responses non supportato.")
                if isinstance(content, list):
                    parts: list[dict[str, Any]] = []
                    for part in content:
                        if not isinstance(part, dict):
                            raise _unsupported("Parte di contenuto Responses non valida.")
                        if part.get("type") in {"input_text", "output_text"}:
                            parts.append({"type": "text", "text": part.get("text")})
                        elif part.get("type") == "input_image":
                            if part.get("file_id"):
                                parts.append({"type": "file", "file": {"file_id": part["file_id"], "image_only": True}})
                                continue
                            parts.append(
                                {
                                    "type": "image_url",
                                    "image_url": {"url": part.get("image_url")},
                                }
                            )
                        elif part.get("type") == "input_file" and isinstance(part.get("file_id"), str):
                            parts.append({"type": "file", "file": {"file_id": part["file_id"], "mode": part.get("mode", "auto")}})
                        else:
                            raise _unsupported("Parte di contenuto Responses non supportata.")
                    content = parts
                if not isinstance(content, (str, list)):
                    raise _unsupported("Contenuto del messaggio Responses non valido.")
                messages.append({"role": role, "content": content})
            elif item_type == "function_call":
                if (
                    not isinstance(item.get("call_id"), str)
                    or not isinstance(item.get("name"), str)
                    or not isinstance(item.get("arguments"), str)
                ):
                    raise _unsupported("Function call Responses non valida.")
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": item["call_id"],
                                "type": "function",
                                "function": {
                                    "name": item["name"],
                                    "arguments": item["arguments"],
                                },
                            }
                        ],
                    }
                )
            elif item_type == "function_call_output":
                if not isinstance(item.get("call_id"), str) or not isinstance(
                    item.get("output"), str
                ):
                    raise _unsupported("Risultato function call Responses non valido.")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item["call_id"],
                        "content": item["output"],
                    }
                )
            else:
                raise _unsupported("Tipo di item Responses non supportato.")

    tools: list[dict[str, Any]] = []
    for tool in payload.tools:
        if tool.get("type") != "function":
            raise _unsupported("Sono supportati solo tool function del client.")
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name"),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object"}),
                },
            }
        )

    choice = payload.tool_choice
    if isinstance(choice, dict):
        if choice.get("type") != "function" or not isinstance(
            choice.get("name"), str
        ):
            raise _unsupported("tool_choice Responses non valido.")
        choice = {"type": "function", "function": {"name": choice["name"]}}

    try:
        return ChatCompletionRequest.model_validate(
            {
            "model": payload.model,
            "messages": messages,
            "tools": tools if tools else None,
            "tool_choice": choice if tools or choice != "auto" else None,
            "stream": payload.stream,
            "stream_options": {"include_usage": True} if payload.stream else None,
            "max_completion_tokens": payload.max_output_tokens,
            "temperature": payload.temperature,
            "top_p": payload.top_p,
            "parallel_tool_calls": False,
            }
        )
    except ValidationError as exc:
        raise _unsupported("Richiesta Responses non valida.", code="invalid_response_input") from exc


def response_shell(response_id: str, model: str, *, created_at: int | None = None) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at or int(time.time()),
        "status": "in_progress",
        "model": model,
        "output": [],
        "output_text": "",
        "usage": None,
        "error": None,
        "incomplete_details": None,
        "store": False,
        "parallel_tool_calls": False,
    }


def input_items(payload: ResponsesRequest) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if payload.instructions:
        items.append({"role": "developer", "content": payload.instructions})
    if isinstance(payload.input, str):
        items.append({"role": "user", "content": payload.input})
    else:
        items.extend(payload.input)
    return items


def from_chat_input(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    items = []
    for message in messages:
        if message.role == "tool":
            items.append({"type": "function_call_output", "call_id": message.tool_call_id, "output": message.text_content()})
            continue
        if message.content is not None:
            content = message.content
            if isinstance(content, list):
                content = [
                    {"type": "input_image", "image_url": part["image_url"]["url"]}
                    if part["type"] == "image_url" else {"type": "input_text", "text": part["text"]}
                    for part in content
                ]
            items.append({"type": "message", "role": message.role, "content": content})
        for call in message.tool_calls or []:
            items.append({"type": "function_call", "call_id": call["id"], "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
    return items


def _output_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        output.append(
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": content, "annotations": []}],
            }
        )
    for call in message.get("tool_calls") or []:
        output.append(
            {
                "id": f"fc_{uuid.uuid4().hex}",
                "type": "function_call",
                "status": "completed",
                "call_id": call["id"],
                "name": call["function"]["name"],
                "arguments": call["function"]["arguments"],
            }
        )
    if not output:
        output.append(
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "", "annotations": []}],
            }
        )
    return output


def from_chat_response(chat: dict[str, Any], response_id: str) -> dict[str, Any]:
    response = response_shell(response_id, chat["model"], created_at=chat["created"])
    response["status"] = "completed"
    if chat["choices"][0].get("finish_reason") == "length":
        response["status"] = "incomplete"
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    response["output"] = _output_items(chat["choices"][0]["message"])
    response["output_text"] = "".join(
        part["text"]
        for item in response["output"]
        if item["type"] == "message"
        for part in item["content"]
        if part["type"] == "output_text"
    )
    usage = chat.get("usage") or {}
    response["usage"] = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }
    return response


async def stream_chat_response(
    chat_stream: StreamingResponse,
    response_id: str,
    model: str,
    *,
    store: bool = False,
    previous_response_id: str | None = None,
) -> AsyncIterator[str]:
    response = response_shell(response_id, model)
    response["store"] = store
    response["previous_response_id"] = previous_response_id
    sequence = 0

    def event(event_type: str, **fields: Any) -> str:
        nonlocal sequence
        sequence += 1
        body = {"type": event_type, "sequence_number": sequence, **fields}
        return f"event: {event_type}\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"

    yield event("response.created", response=response.copy())
    yield event("response.in_progress", response=response.copy())

    message_id: str | None = None
    message_index: int | None = None
    content = ""
    terminated = False
    incomplete = False
    async for raw in chat_stream.body_iterator:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        for line in raw.splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            if "error" in chunk:
                response["status"] = "failed"
                response["error"] = chunk["error"]
                yield event("response.failed", response=response.copy())
                terminated = True
                break
            if not chunk.get("choices"):
                usage = chunk.get("usage") or {}
                response["usage"] = {
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                continue
            delta = chunk["choices"][0].get("delta") or {}
            if chunk["choices"][0].get("finish_reason") == "length":
                incomplete = True
            text = delta.get("content")
            if isinstance(text, str) and text:
                if message_id is None:
                    message_id = f"msg_{uuid.uuid4().hex}"
                    message_index = len(response["output"])
                    item = {"id": message_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []}
                    response["output"].append(item)
                    yield event("response.output_item.added", output_index=message_index, item=item.copy())
                    yield event("response.content_part.added", item_id=message_id, output_index=message_index, content_index=0, part={"type": "output_text", "text": "", "annotations": []})
                content += text
                yield event("response.output_text.delta", item_id=message_id, output_index=message_index, content_index=0, delta=text)
            for call in delta.get("tool_calls") or []:
                function = call.get("function") or {}
                item = {"id": f"fc_{uuid.uuid4().hex}", "type": "function_call", "status": "in_progress", "call_id": call.get("id"), "name": function.get("name"), "arguments": ""}
                index = len(response["output"])
                yield event("response.output_item.added", output_index=index, item=item.copy())
                item["arguments"] = function.get("arguments", "")
                yield event("response.function_call_arguments.delta", item_id=item["id"], output_index=index, delta=item["arguments"])
                yield event("response.function_call_arguments.done", item_id=item["id"], output_index=index, arguments=item["arguments"])
                item["status"] = "completed"
                response["output"].append(item)
                yield event("response.output_item.done", output_index=index, item=item.copy())
        if terminated:
            break
    if terminated:
        return
    if message_id is not None and message_index is not None:
        index = message_index
        part = {"type": "output_text", "text": content, "annotations": []}
        yield event("response.output_text.done", item_id=message_id, output_index=index, content_index=0, text=content)
        yield event("response.content_part.done", item_id=message_id, output_index=index, content_index=0, part=part)
        item = {"id": message_id, "type": "message", "role": "assistant", "status": "completed", "content": [part]}
        response["output"][index] = item
        yield event("response.output_item.done", output_index=index, item=item)
        response["output_text"] = content
    if not response["output"]:
        response["output"] = _output_items({"content": ""})
    response["status"] = "incomplete" if incomplete else "completed"
    if incomplete:
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    yield event("response.incomplete" if incomplete else "response.completed", response=response.copy())
    yield "data: [DONE]\n\n"
