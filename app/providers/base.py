from collections.abc import AsyncIterator
from typing import Protocol

from app.schemas import ChatCompletionRequest


class TextProvider(Protocol):
    """Minimum contract shared by local, broker and upstream adapters."""

    async def complete(
        self, request: ChatCompletionRequest, resolved_model: str
    ) -> str: ...

    def stream(
        self, request: ChatCompletionRequest, resolved_model: str
    ) -> AsyncIterator[str]: ...
