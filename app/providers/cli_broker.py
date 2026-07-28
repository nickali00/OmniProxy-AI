from __future__ import annotations

from collections.abc import AsyncIterator

from app.errors import GatewayError
from app.provider_broker import ProviderBrokerClient, ProviderBrokerError
from app.routing import ModelRoute
from app.schemas import ChatCompletionRequest


class CliBrokerProvider:
    """Adapter comune per i client ufficiali nei sidecar cloud."""

    def __init__(
        self,
        client: ProviderBrokerClient,
        route: ModelRoute,
    ) -> None:
        self._client = client
        self._route = route

    async def complete(
        self,
        request: ChatCompletionRequest,
        resolved_model: str,
    ) -> str:
        try:
            return await self._client.complete(
                request,
                model=resolved_model,
                reasoning_effort=self._route.reasoning_effort,
            )
        except ProviderBrokerError as exc:
            raise GatewayError(
                exc.status_code,
                str(exc),
                error_type="provider_error",
                code=exc.code,
            ) from exc

    async def stream(
        self,
        request: ChatCompletionRequest,
        resolved_model: str,
    ) -> AsyncIterator[str]:
        # I CLI ufficiali sono invocati in modalità headless non interattiva.
        # Per mantenere la compatibilità SSE del gateway, il risultato completo
        # viene suddiviso in frammenti senza esporre gli eventi interni agentici.
        response = await self.complete(request, resolved_model)
        words = response.split(" ")
        for index, word in enumerate(words):
            if index:
                yield " "
            yield word
