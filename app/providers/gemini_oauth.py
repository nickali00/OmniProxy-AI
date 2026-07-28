from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from app.config import settings
from app.database import (
    clear_provider_tokens,
    get_provider_connection,
    save_provider_configuration,
    save_provider_tokens,
)
from app.provider_vault import ProviderVault, ProviderVaultError
from app.schemas import ChatCompletionRequest, GeminiOAuthConfiguration


GOOGLE_OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/generative-language.retriever",
)
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
GOOGLE_AUTH_PATHS = frozenset(
    {
        "/o/oauth2/auth",
        "/o/oauth2/v2/auth",
    }
)


class GeminiOAuthClientFileError(ValueError):
    pass


def configuration_from_google_client_file(
    document: object,
) -> GeminiOAuthConfiguration:
    """
    Estrae la configurazione da un client_secret.json Google Desktop.

    Il documento originale non viene persistito. Accettiamo esclusivamente il
    formato `installed` e verifichiamo che authorization endpoint, token
    endpoint e redirect siano quelli previsti da Google. Gli errori non
    includono mai valori provenienti dal file, così il client secret non può
    finire nella risposta o nei log.
    """

    if not isinstance(document, dict):
        raise GeminiOAuthClientFileError(
            "Il file selezionato non è un client OAuth Google valido."
        )

    installed = document.get("installed")
    if not isinstance(installed, dict):
        raise GeminiOAuthClientFileError(
            "Scarica un client OAuth Google di tipo Applicazione desktop."
        )

    if not _is_official_google_endpoint(
        installed.get("auth_uri"),
        hostname="accounts.google.com",
        paths=GOOGLE_AUTH_PATHS,
    ) or not _is_official_google_endpoint(
        installed.get("token_uri"),
        hostname="oauth2.googleapis.com",
        paths=frozenset({"/token"}),
    ):
        raise GeminiOAuthClientFileError(
            "Il file OAuth non usa gli endpoint ufficiali Google."
        )

    redirect_uris = installed.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not any(
        _is_loopback_redirect(value) for value in redirect_uris
    ):
        raise GeminiOAuthClientFileError(
            "Il client OAuth non contiene una callback locale valida."
        )

    try:
        return GeminiOAuthConfiguration(
            project_id=installed.get("project_id"),
            client_id=installed.get("client_id"),
            client_secret=installed.get("client_secret"),
        )
    except ValueError as exc:
        raise GeminiOAuthClientFileError(
            "Il file OAuth Google non contiene credenziali valide."
        ) from exc


def _is_official_google_endpoint(
    value: object,
    *,
    hostname: str,
    paths: frozenset[str],
) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == hostname
        and port is None
        and parsed.path in paths
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def _is_loopback_redirect(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


class GeminiProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 503,
        code: str = "gemini_provider_unavailable",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass
class OAuthAttempt:
    id: str
    state: str
    code_verifier: str
    redirect_uri: str
    expires_at: float
    status: str = "waiting_for_user"

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "state": self.status,
            "expires_at": datetime.fromtimestamp(
                self.expires_at,
                tz=UTC,
            ).isoformat(),
            "requires_code": False,
        }


class GeminiOAuthProvider:
    """
    Adapter Gemini API con OAuth nativo.

    Non usa sessioni di VS Code, Gemini CLI o Antigravity. Il browser parla
    soltanto con Google; client secret, verifier PKCE e token restano nel
    gateway e vengono cifrati prima della persistenza.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        vault: ProviderVault | None = None,
    ) -> None:
        self._http = http_client
        self._vault = vault or ProviderVault()
        self._attempts_by_id: dict[str, OAuthAttempt] = {}
        self._attempt_ids_by_state: dict[str, str] = {}
        self._refresh_lock = asyncio.Lock()

    async def status(self, redirect_uri: str) -> dict[str, object]:
        self._purge_expired_attempts()
        record = await get_provider_connection("gemini")
        if record is None:
            return {
                "provider": "gemini",
                "installed": True,
                "configured": False,
                "connected": False,
                "auth_method": "oauth_2_pkce",
                "client_mode": "native_api",
                "redirect_uri": redirect_uri,
                "attempt": self._active_public_attempt(),
            }

        try:
            configuration = self._vault.decrypt_json(
                record.configuration_ciphertext
            )
            tokens = (
                self._vault.decrypt_json(record.tokens_ciphertext)
                if record.tokens_ciphertext
                else None
            )
        except ProviderVaultError:
            return {
                "provider": "gemini",
                "installed": True,
                "configured": True,
                "connected": False,
                "auth_method": "oauth_2_pkce",
                "client_mode": "native_api",
                "connection_error": "vault_unavailable",
                "redirect_uri": redirect_uri,
                "attempt": self._active_public_attempt(),
            }

        connected = self._tokens_are_usable(tokens)
        client_id = str(configuration.get("client_id", ""))
        return {
            "provider": "gemini",
            "installed": True,
            "configured": True,
            "connected": connected,
            "auth_method": "oauth_2_pkce",
            "client_mode": "native_api",
            "project_id": str(configuration.get("project_id", "")),
            "client_id_hint": self._client_id_hint(client_id),
            "redirect_uri": redirect_uri,
            "connected_at": record.connected_at,
            "attempt": self._active_public_attempt(),
        }

    async def configure(
        self,
        configuration: GeminiOAuthConfiguration,
    ) -> None:
        previous = await get_provider_connection("gemini")
        if previous and previous.tokens_ciphertext:
            await self._revoke_ciphertext_best_effort(
                previous.configuration_ciphertext,
                previous.tokens_ciphertext,
            )
        try:
            encrypted = self._vault.encrypt_json(configuration.model_dump())
        except ProviderVaultError as exc:
            raise GeminiProviderError(
                "Il vault delle credenziali Gemini non può essere inizializzato.",
                status_code=503,
                code="provider_vault_unavailable",
            ) from exc
        await save_provider_configuration("gemini", encrypted)
        self._attempts_by_id.clear()
        self._attempt_ids_by_state.clear()

    async def start_auth(self, redirect_uri: str) -> dict[str, object]:
        self._purge_expired_attempts()
        record, configuration, tokens = await self._load_connection()
        if record is None or configuration is None:
            raise GeminiProviderError(
                "Configura prima il progetto OAuth Google dalla dashboard.",
                status_code=409,
                code="provider_configuration_required",
            )
        if self._tokens_are_usable(tokens):
            return {
                "provider": "gemini",
                "installed": True,
                "configured": True,
                "connected": True,
                "auth_method": "oauth_2_pkce",
                "attempt": None,
            }

        existing = self._active_attempt()
        if existing is not None:
            if hmac.compare_digest(existing.redirect_uri, redirect_uri):
                return self._auth_payload(existing, configuration)
            self._remove_attempt(existing)

        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        attempt = OAuthAttempt(
            id=str(uuid.uuid4()),
            state=secrets.token_urlsafe(32),
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            expires_at=time.time() + settings.gemini_oauth_ttl_seconds,
        )
        self._attempts_by_id[attempt.id] = attempt
        self._attempt_ids_by_state[attempt.state] = attempt.id
        return self._auth_payload(attempt, configuration, challenge)

    async def complete_callback(self, *, state: str, code: str) -> None:
        self._purge_expired_attempts()
        attempt_id = self._attempt_ids_by_state.get(state)
        attempt = (
            self._attempts_by_id.get(attempt_id)
            if attempt_id is not None
            else None
        )
        if (
            attempt is None
            or not hmac.compare_digest(attempt.state, state)
            or attempt.status != "waiting_for_user"
        ):
            raise GeminiProviderError(
                "La richiesta OAuth è invalida o scaduta.",
                status_code=400,
                code="invalid_oauth_state",
            )
        if not 8 <= len(code) <= 8192 or any(
            value in code for value in ("\r", "\n", "\x00")
        ):
            attempt.status = "failed"
            raise GeminiProviderError(
                "Il codice restituito da Google non è valido.",
                status_code=400,
                code="invalid_oauth_code",
            )

        record, configuration, _ = await self._load_connection()
        if record is None or configuration is None:
            attempt.status = "failed"
            raise GeminiProviderError(
                "La configurazione OAuth non è più disponibile.",
                status_code=409,
                code="provider_configuration_required",
            )

        attempt.status = "verifying"
        try:
            response = await self._http.post(
                settings.gemini_oauth_token_url,
                data={
                    "client_id": configuration["client_id"],
                    "client_secret": configuration["client_secret"],
                    "code": code,
                    "code_verifier": attempt.code_verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": attempt.redirect_uri,
                },
                headers={"Accept": "application/json"},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            attempt.status = "failed"
            raise GeminiProviderError(
                "Google OAuth non è raggiungibile.",
                code="oauth_exchange_unavailable",
            ) from exc

        payload = self._json_object(response)
        if response.is_error:
            attempt.status = "failed"
            raise GeminiProviderError(
                "Google non ha accettato il collegamento OAuth.",
                status_code=400,
                code="oauth_exchange_failed",
            )
        try:
            tokens = self._normalize_token_payload(payload)
        except GeminiProviderError:
            attempt.status = "failed"
            raise
        if not tokens.get("refresh_token"):
            attempt.status = "failed"
            raise GeminiProviderError(
                "Google non ha restituito un refresh token. Revoca il consenso "
                "precedente e ripeti il collegamento.",
                status_code=400,
                code="oauth_refresh_token_missing",
            )
        await save_provider_tokens(
            "gemini",
            self._vault.encrypt_json(tokens),
        )
        self._remove_attempt(attempt)

    async def reject_callback(self, state: str) -> None:
        attempt_id = self._attempt_ids_by_state.get(state)
        attempt = (
            self._attempts_by_id.get(attempt_id)
            if attempt_id is not None
            else None
        )
        if attempt is not None and hmac.compare_digest(attempt.state, state):
            attempt.status = "failed"

    async def cancel_auth(self, attempt_id: str) -> None:
        attempt = self._attempts_by_id.get(attempt_id)
        if attempt is not None:
            attempt.status = "cancelled"
            self._remove_attempt(attempt)

    async def disconnect(self) -> None:
        record = await get_provider_connection("gemini")
        if record and record.tokens_ciphertext:
            await self._revoke_ciphertext_best_effort(
                record.configuration_ciphertext,
                record.tokens_ciphertext,
            )
        await clear_provider_tokens("gemini")
        self._attempts_by_id.clear()
        self._attempt_ids_by_state.clear()

    async def models(self) -> dict[str, object]:
        access_token, configuration = await self._access_token()
        response = await self._authorized_request(
            "GET",
            f"{settings.gemini_api_base_url.rstrip('/')}/v1beta/models",
            access_token=access_token,
            configuration=configuration,
            params={"pageSize": "1000"},
        )
        payload = self._json_object(response)
        if response.status_code == 401:
            access_token, configuration = await self._access_token(
                force_refresh=True
            )
            response = await self._authorized_request(
                "GET",
                f"{settings.gemini_api_base_url.rstrip('/')}/v1beta/models",
                access_token=access_token,
                configuration=configuration,
                params={"pageSize": "1000"},
            )
            payload = self._json_object(response)
        if response.is_error:
            self._raise_api_error(response)

        models = self._normalize_models(payload.get("models"))
        return {
            "provider": "gemini",
            "connected": True,
            "models": models,
        }

    async def complete(
        self,
        request: ChatCompletionRequest,
        resolved_model: str,
        *,
        reasoning_effort: str = "auto",
    ) -> str:
        if not MODEL_ID_PATTERN.fullmatch(resolved_model):
            raise GeminiProviderError(
                "Il modello Gemini selezionato non è valido.",
                status_code=400,
                code="invalid_gemini_model",
            )
        body = self._generate_content_body(request, reasoning_effort)
        access_token, configuration = await self._access_token()
        url = (
            f"{settings.gemini_api_base_url.rstrip('/')}/v1beta/models/"
            f"{resolved_model}:generateContent"
        )
        response = await self._authorized_request(
            "POST",
            url,
            access_token=access_token,
            configuration=configuration,
            json=body,
        )
        if response.status_code == 401:
            access_token, configuration = await self._access_token(
                force_refresh=True
            )
            response = await self._authorized_request(
                "POST",
                url,
                access_token=access_token,
                configuration=configuration,
                json=body,
            )
        payload = self._json_object(response)
        if response.is_error:
            self._raise_api_error(response)
        content = self._response_text(payload)
        if not content:
            raise GeminiProviderError(
                "Gemini non ha restituito una risposta testuale.",
                status_code=502,
                code="empty_provider_completion",
            )
        return content

    async def stream(
        self,
        request: ChatCompletionRequest,
        resolved_model: str,
        *,
        reasoning_effort: str = "auto",
    ) -> AsyncIterator[str]:
        content = await self.complete(
            request,
            resolved_model,
            reasoning_effort=reasoning_effort,
        )
        for index, word in enumerate(content.split(" ")):
            if index:
                yield " "
            yield word

    async def _load_connection(
        self,
    ) -> tuple[object | None, dict[str, Any] | None, dict[str, Any] | None]:
        record = await get_provider_connection("gemini")
        if record is None:
            return None, None, None
        try:
            configuration = self._vault.decrypt_json(
                record.configuration_ciphertext
            )
            tokens = (
                self._vault.decrypt_json(record.tokens_ciphertext)
                if record.tokens_ciphertext
                else None
            )
        except ProviderVaultError as exc:
            raise GeminiProviderError(
                "Il vault delle credenziali Gemini non è disponibile.",
                status_code=503,
                code="provider_vault_unavailable",
            ) from exc
        return record, configuration, tokens

    async def _access_token(
        self,
        *,
        force_refresh: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        async with self._refresh_lock:
            record, configuration, tokens = await self._load_connection()
            if record is None or configuration is None or tokens is None:
                raise GeminiProviderError(
                    "Collega l'account Google prima di usare Gemini.",
                    status_code=409,
                    code="provider_not_connected",
                )
            access_token = tokens.get("access_token")
            expires_at = tokens.get("expires_at")
            if (
                not force_refresh
                and isinstance(access_token, str)
                and isinstance(expires_at, (int, float))
                and expires_at > time.time() + 60
            ):
                return access_token, configuration

            refresh_token = tokens.get("refresh_token")
            if not isinstance(refresh_token, str) or not refresh_token:
                await clear_provider_tokens("gemini")
                raise GeminiProviderError(
                    "La sessione Google è scaduta. Collega nuovamente Gemini.",
                    status_code=409,
                    code="provider_reauthentication_required",
                )
            try:
                response = await self._http.post(
                    settings.gemini_oauth_token_url,
                    data={
                        "client_id": configuration["client_id"],
                        "client_secret": configuration["client_secret"],
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                    headers={"Accept": "application/json"},
                    timeout=30.0,
                )
            except httpx.HTTPError as exc:
                raise GeminiProviderError(
                    "Google OAuth non è raggiungibile.",
                    code="oauth_refresh_unavailable",
                ) from exc
            payload = self._json_object(response)
            if response.is_error:
                if payload.get("error") == "invalid_grant":
                    await clear_provider_tokens("gemini")
                    raise GeminiProviderError(
                        "La sessione Google è stata revocata. Ricollega Gemini.",
                        status_code=409,
                        code="provider_reauthentication_required",
                    )
                raise GeminiProviderError(
                    "Google non ha rinnovato la sessione OAuth.",
                    status_code=502,
                    code="oauth_refresh_failed",
                )
            refreshed = self._normalize_token_payload(
                payload,
                fallback_refresh_token=refresh_token,
            )
            await save_provider_tokens(
                "gemini",
                self._vault.encrypt_json(refreshed),
            )
            return str(refreshed["access_token"]), configuration

    async def _authorized_request(
        self,
        method: str,
        url: str,
        *,
        access_token: str,
        configuration: dict[str, Any],
        **kwargs: object,
    ) -> httpx.Response:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "x-goog-user-project": str(configuration["project_id"]),
        }
        try:
            return await self._http.request(
                method,
                url,
                headers=headers,
                timeout=settings.provider_completion_timeout_seconds,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise GeminiProviderError(
                "La Gemini API non è raggiungibile.",
                code="gemini_api_unavailable",
            ) from exc

    async def _revoke_ciphertext_best_effort(
        self,
        configuration_ciphertext: str,
        tokens_ciphertext: str,
    ) -> None:
        try:
            tokens = self._vault.decrypt_json(tokens_ciphertext)
            token = tokens.get("refresh_token") or tokens.get("access_token")
            if not isinstance(token, str) or not token:
                return
            await self._http.post(
                settings.gemini_oauth_revoke_url,
                data={"token": token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10.0,
            )
        except (ProviderVaultError, httpx.HTTPError):
            return

    def _auth_payload(
        self,
        attempt: OAuthAttempt,
        configuration: dict[str, Any],
        challenge: str | None = None,
    ) -> dict[str, object]:
        if challenge is None:
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(
                    attempt.code_verifier.encode("ascii")
                ).digest()
            ).rstrip(b"=").decode("ascii")
        query = urlencode(
            {
                "access_type": "offline",
                "client_id": configuration["client_id"],
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "include_granted_scopes": "true",
                "prompt": "consent",
                "redirect_uri": attempt.redirect_uri,
                "response_type": "code",
                "scope": " ".join(GOOGLE_OAUTH_SCOPES),
                "state": attempt.state,
            }
        )
        return {
            "provider": "gemini",
            "installed": True,
            "configured": True,
            "connected": False,
            "auth_method": "oauth_2_pkce",
            "auth_url": f"{settings.gemini_oauth_authorize_url}?{query}",
            "attempt": attempt.public(),
        }

    def _active_attempt(self) -> OAuthAttempt | None:
        for attempt in self._attempts_by_id.values():
            if attempt.status in {"waiting_for_user", "verifying"}:
                return attempt
        return None

    def _active_public_attempt(self) -> dict[str, object] | None:
        attempt = self._active_attempt()
        if attempt is None and self._attempts_by_id:
            attempt = next(reversed(self._attempts_by_id.values()))
        return attempt.public() if attempt else None

    def _purge_expired_attempts(self) -> None:
        now = time.time()
        for attempt in list(self._attempts_by_id.values()):
            if attempt.expires_at <= now:
                self._remove_attempt(attempt)

    def _remove_attempt(self, attempt: OAuthAttempt) -> None:
        self._attempts_by_id.pop(attempt.id, None)
        current_id = self._attempt_ids_by_state.get(attempt.state)
        if current_id == attempt.id:
            self._attempt_ids_by_state.pop(attempt.state, None)

    @staticmethod
    def _tokens_are_usable(tokens: dict[str, Any] | None) -> bool:
        if not isinstance(tokens, dict):
            return False
        refresh_token = tokens.get("refresh_token")
        if isinstance(refresh_token, str) and bool(refresh_token):
            return True
        access_token = tokens.get("access_token")
        expires_at = tokens.get("expires_at")
        return (
            isinstance(access_token, str)
            and bool(access_token)
            and isinstance(expires_at, (int, float))
            and expires_at > time.time() + 60
        )

    @staticmethod
    def _client_id_hint(client_id: str) -> str:
        if len(client_id) <= 18:
            return client_id
        return f"{client_id[:10]}…{client_id[-8:]}"

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _normalize_token_payload(
        payload: dict[str, Any],
        *,
        fallback_refresh_token: str | None = None,
    ) -> dict[str, Any]:
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise GeminiProviderError(
                "Google OAuth ha restituito token non validi.",
                status_code=502,
                code="invalid_oauth_token_response",
            )
        expires_in = payload.get("expires_in", 3600)
        if not isinstance(expires_in, (int, float)):
            expires_in = 3600
        refresh_token = payload.get("refresh_token", fallback_refresh_token)
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": time.time() + max(60, float(expires_in)),
            "scope": payload.get("scope", ""),
            "token_type": payload.get("token_type", "Bearer"),
        }

    @staticmethod
    def _normalize_models(value: object) -> list[dict[str, object]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, object]] = []
        for raw_model in value[:300]:
            if not isinstance(raw_model, dict):
                continue
            methods = raw_model.get("supportedGenerationMethods")
            if not isinstance(methods, list) or "generateContent" not in methods:
                continue
            raw_name = raw_model.get("name")
            if not isinstance(raw_name, str):
                continue
            model_id = raw_name.removeprefix("models/")
            if not MODEL_ID_PATTERN.fullmatch(model_id):
                continue
            efforts = GeminiOAuthProvider._reasoning_efforts(model_id)
            normalized.append(
                {
                    "id": model_id,
                    "display_name": str(
                        raw_model.get("displayName") or model_id
                    )[:100],
                    "description": str(
                        raw_model.get("description")
                        or "Modello disponibile nella Gemini API."
                    )[:300],
                    "is_default": False,
                    "reasoning_efforts": efforts,
                    "default_reasoning_effort": "auto",
                }
            )
        preferred = (
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3-flash-preview",
            "gemini-3.1-pro-preview",
        )
        preferred_rank = {
            model_id: index for index, model_id in enumerate(preferred)
        }
        normalized.sort(
            key=lambda item: (
                preferred_rank.get(str(item["id"]), len(preferred)),
                str(item["display_name"]).lower(),
            )
        )
        if normalized:
            normalized[0]["is_default"] = True
        return normalized[:100]

    @staticmethod
    def _reasoning_efforts(model_id: str) -> list[str]:
        if not model_id.startswith("gemini-3"):
            return ["auto"]
        if "pro" in model_id:
            return ["auto", "low", "medium", "high"]
        return ["auto", "minimal", "low", "medium", "high"]

    @staticmethod
    def _generate_content_body(
        request: ChatCompletionRequest,
        reasoning_effort: str,
    ) -> dict[str, object]:
        system_parts: list[str] = []
        contents: list[dict[str, object]] = []
        for message in request.messages:
            text = message.text_content().strip()
            if not text:
                continue
            if message.role in {"system", "developer"}:
                system_parts.append(text)
                continue
            role = "model" if message.role == "assistant" else "user"
            if contents and contents[-1]["role"] == role:
                parts = contents[-1]["parts"]
                if isinstance(parts, list):
                    parts.append({"text": text})
            else:
                contents.append({"role": role, "parts": [{"text": text}]})
        if not contents:
            raise GeminiProviderError(
                "La richiesta non contiene messaggi utilizzabili da Gemini.",
                status_code=400,
                code="invalid_gemini_messages",
            )

        generation_config: dict[str, object] = {}
        if request.output_token_limit is not None:
            generation_config["maxOutputTokens"] = request.output_token_limit
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if request.top_p is not None:
            generation_config["topP"] = request.top_p
        if request.stop is not None:
            generation_config["stopSequences"] = (
                [request.stop] if isinstance(request.stop, str) else request.stop
            )
        if request.seed is not None:
            generation_config["seed"] = request.seed
        if reasoning_effort != "auto":
            if reasoning_effort not in {"minimal", "low", "medium", "high"}:
                raise GeminiProviderError(
                    "Il livello di reasoning Gemini non è valido.",
                    status_code=400,
                    code="invalid_reasoning_effort",
                )
            generation_config["thinkingConfig"] = {
                "thinkingLevel": reasoning_effort
            }

        body: dict[str, object] = {"contents": contents}
        if system_parts:
            body["systemInstruction"] = {
                "parts": [{"text": "\n\n".join(system_parts)}]
            }
        if generation_config:
            body["generationConfig"] = generation_config
        return body

    @staticmethod
    def _response_text(payload: dict[str, Any]) -> str:
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return ""
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            return ""
        content = candidate.get("content")
        if not isinstance(content, dict):
            return ""
        parts = content.get("parts")
        if not isinstance(parts, list):
            return ""
        text_parts = [
            part["text"]
            for part in parts
            if isinstance(part, dict)
            and part.get("thought") is not True
            and isinstance(part.get("text"), str)
        ]
        return "".join(text_parts).strip()

    @staticmethod
    def _raise_api_error(response: httpx.Response) -> None:
        if response.status_code == 401:
            raise GeminiProviderError(
                "La sessione Gemini non è più valida.",
                status_code=409,
                code="provider_reauthentication_required",
            )
        if response.status_code == 403:
            raise GeminiProviderError(
                "Il progetto Google non è autorizzato a usare la Gemini API.",
                status_code=403,
                code="gemini_project_not_authorized",
            )
        if response.status_code == 429:
            raise GeminiProviderError(
                "La quota Gemini API è temporaneamente esaurita.",
                status_code=429,
                code="provider_rate_limit",
            )
        if response.status_code == 400:
            raise GeminiProviderError(
                "Gemini ha rifiutato i parametri della richiesta.",
                status_code=400,
                code="invalid_provider_request",
            )
        raise GeminiProviderError(
            "La Gemini API non ha completato la richiesta.",
            status_code=502,
            code="gemini_api_error",
        )
