import json
from functools import lru_cache

import tiktoken

from app.config import settings
from app.schemas import ChatCompletionRequest, ChatMessage


@lru_cache(maxsize=4)
def _encoding(name: str) -> tiktoken.Encoding:
    try:
        return tiktoken.get_encoding(name)
    except ValueError:
        return tiktoken.get_encoding("cl100k_base")


def count_text_tokens(text: str) -> int:
    if not text:
        return 0
    return len(
        _encoding(settings.tiktoken_encoding).encode(
            text,
            disallowed_special=(),
        )
    )


def count_message_tokens(messages: list[ChatMessage]) -> int:
    """
    Count a stable, provider-independent representation of the chat payload.

    Ollama models can use tokenizers different from tiktoken, so this is an
    accounting estimate, not the model provider's authoritative usage value.
    """
    # Never tokenize base64: it is neither model text nor a useful usage
    # estimate. Vision tokenization varies by provider and image resolution,
    # so account for a stable metadata marker instead.
    total = 0
    for message in messages:
        total += count_text_tokens(message.role)
        total += count_text_tokens(message.text_content())
        if message.tool_calls:
            total += count_text_tokens(json.dumps(message.tool_calls, ensure_ascii=False, sort_keys=True))
        if message.tool_call_id:
            total += count_text_tokens(message.tool_call_id)
        if message.name:
            total += count_text_tokens(message.name)
        for image in message.image_payloads():
            marker = f"[image:{image.mime_type}:{len(image.data)}_bytes]"
            total += count_text_tokens(marker)
    return total


def count_request_tokens(request: ChatCompletionRequest) -> int:
    total = count_message_tokens(request.messages)
    if request.tools:
        total += count_text_tokens(json.dumps(request.tools, ensure_ascii=False, sort_keys=True))
    return total


def clip_text_tokens(text: str, limit: int) -> str:
    encoding = _encoding(settings.tiktoken_encoding)
    tokens = encoding.encode(text, disallowed_special=())
    return encoding.decode_bytes(tokens[:limit]).decode("utf-8", errors="ignore")
