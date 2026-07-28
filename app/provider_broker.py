from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import httpx

from app.config import settings
from app.schemas import ChatCompletionRequest


class ProviderBrokerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 503,
        code: str = "provider_broker_unavailable",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class ProviderBrokerClient:
    """Contratto HTTP comune per i sidecar provider autonomi."""

    provider_name = "Provider"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def status(self) -> dict[str, object]:
        return await self._request("GET", "/v1/status")

    async def start_auth(self) -> dict[str, object]:
        payload = await self._request("POST", "/v1/auth/start")
        auth_url = payload.get("auth_url")
        if auth_url is not None and not self._is_allowed_auth_url(auth_url):
            raise ProviderBrokerError(
                "Il provider ha restituito una destinazione OAuth non consentita.",
                status_code=502,
                code="invalid_provider_auth_url",
            )
        return payload

    async def models(self) -> dict[str, object]:
        return await self._request("GET", "/v1/models")

    async def quota(self) -> dict[str, object]:
        return await self._request("GET", "/v1/quota")

    async def complete(
        self,
        request: ChatCompletionRequest,
        *,
        model: str,
        reasoning_effort: str,
    ) -> str:
        payload = await self._request(
            "POST",
            "/v1/chat",
            json={
                "model": model,
                "reasoning_effort": reasoning_effort,
                "messages": [
                    {
                        "role": message.role,
                        "content": message.text_content(),
                    }
                    for message in request.messages
                ],
                "max_output_tokens": request.output_token_limit,
                "temperature": request.temperature,
            },
            timeout=settings.provider_completion_timeout_seconds,
        )
        content = payload.get("content")
        if not isinstance(content, str):
            raise ProviderBrokerError(
                f"Il broker {self.provider_name} ha restituito "
                "un completamento non valido.",
                status_code=502,
                code="invalid_provider_completion",
            )
        return content

    async def submit_code(
        self,
        attempt_id: str,
        code: str,
    ) -> dict[str, object]:
        self._validate_attempt_id(attempt_id)
        return await self._request(
            "POST",
            f"/v1/auth/{attempt_id}/code",
            json={"code": code},
        )

    async def cancel_auth(self, attempt_id: str) -> dict[str, object]:
        self._validate_attempt_id(attempt_id)
        return await self._request("DELETE", f"/v1/auth/{attempt_id}")

    async def disconnect(self) -> dict[str, object]:
        return await self._request("DELETE", "/v1/connection")

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: object,
    ) -> dict[str, object]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderBrokerError(
                f"Il broker {self.provider_name} non è raggiungibile.",
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderBrokerError(
                f"Il broker {self.provider_name} ha restituito "
                "una risposta non valida.",
                status_code=502,
            ) from exc

        if not isinstance(payload, dict):
            raise ProviderBrokerError(
                f"Il broker {self.provider_name} ha restituito "
                "una risposta non valida.",
                status_code=502,
            )

        if response.is_error:
            message = payload.get("message")
            code = payload.get("error")
            raise ProviderBrokerError(
                message
                if isinstance(message, str)
                else (
                    f"Il broker {self.provider_name} non ha completato "
                    "l'operazione."
                ),
                status_code=response.status_code,
                code=code if isinstance(code, str) else "provider_broker_error",
            )
        return payload

    @staticmethod
    def _validate_attempt_id(attempt_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", attempt_id):
            raise ProviderBrokerError(
                "Identificatore del tentativo di accesso non valido.",
                status_code=400,
                code="invalid_auth_attempt",
            )

    @staticmethod
    def _parsed_https_url(value: object):
        if not isinstance(value, str):
            return None
        parsed = urlparse(value)
        try:
            port = parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme != "https"
            or port not in {None, 443}
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            return None
        return parsed

    def _is_allowed_auth_url(self, value: object) -> bool:
        raise NotImplementedError


class ClaudeBrokerClient(ProviderBrokerClient):
    provider_name = "Claude"

    def _is_allowed_auth_url(self, value: object) -> bool:
        parsed = self._parsed_https_url(value)
        return bool(
            parsed
            and parsed.hostname == "claude.com"
            and parsed.path == "/cai/oauth/authorize"
            and parsed.query
        )


class CodexBrokerClient(ProviderBrokerClient):
    provider_name = "Codex"
    _allowed_hosts = {
        "auth.openai.com",
        "chatgpt.com",
        "device.openai.com",
    }

    def _is_allowed_auth_url(self, value: object) -> bool:
        parsed = self._parsed_https_url(value)
        return bool(
            parsed
            and parsed.hostname in self._allowed_hosts
            and parsed.path not in {"", "/"}
        )


class AntigravityBrokerClient(ProviderBrokerClient):
    provider_name = "Antigravity"

    def _is_allowed_auth_url(self, value: object) -> bool:
        parsed = self._parsed_https_url(value)
        if not parsed or parsed.hostname != "accounts.google.com":
            return False
        if parsed.path not in {"/o/oauth2/auth", "/o/oauth2/v2/auth"}:
            return False
        query = parse_qs(parsed.query)
        challenge = query.get("code_challenge", [""])[0]
        state = query.get("state", [""])[0]
        return (
            query.get("redirect_uri")
            == ["https://antigravity.google/oauth-callback"]
            and query.get("response_type") == ["code"]
            and query.get("code_challenge_method") == ["S256"]
            and bool(re.fullmatch(r"[A-Za-z0-9_-]{43,128}", challenge))
            and 8 <= len(state) <= 1024
        )
