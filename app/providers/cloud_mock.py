import asyncio
from collections.abc import AsyncIterator

from app.config import settings
from app.schemas import ChatCompletionRequest


class ExternalReasoningMockProvider:
    """
    Placeholder for a future OpenAI/Gemini adapter.

    Replace this class with an official SDK-backed adapter. Cloud credentials,
    provider URLs and concrete model names must remain server-side.
    """

    async def complete(
        self,
        request: ChatCompletionRequest,
        resolved_model: str,
    ) -> str:
        await asyncio.sleep(settings.external_mock_latency_seconds)
        prompt = next(
            (
                message.text_content()
                for message in reversed(request.messages)
                if message.role == "user"
            ),
            "",
        )
        preview = prompt[:160].replace("\n", " ").strip()
        suffix = f' Prompt ricevuto: "{preview}".' if preview else ""
        return (
            "[MOCK reasoning-avanzato] Il provider cloud non è ancora "
            "configurato; il punto di integrazione è pronto."
            f"{suffix}"
        )

    async def stream(
        self,
        request: ChatCompletionRequest,
        resolved_model: str,
    ) -> AsyncIterator[str]:
        response = await self.complete(request, resolved_model)
        words = response.split(" ")
        for index, word in enumerate(words):
            if index:
                yield " "
            yield word
            await asyncio.sleep(0)
