import json
from functools import lru_cache

import tiktoken

from app.config import settings
from app.schemas import ChatMessage


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
    serializable = [
        message.model_dump(mode="json", exclude_none=True) for message in messages
    ]
    canonical_json = json.dumps(
        serializable,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return count_text_tokens(canonical_json)
