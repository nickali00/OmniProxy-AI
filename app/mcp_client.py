from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.config import settings
from app.database import McpServerRecord
from app.provider_vault import ProviderVault, ProviderVaultError


class McpClientError(RuntimeError):
    """Safe error raised by the gateway-side MCP client."""


class McpToolError(McpClientError):
    """An MCP tool returned an error result."""


@dataclass(frozen=True)
class McpDiscovery:
    protocol_version: str | None
    server_info: dict[str, object] | None
    tools: list[dict[str, object]]


def normalize_mcp_url(value: str) -> str:
    """Accept only explicit Streamable HTTP endpoints without URL secrets."""
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("La porta del server MCP non è valida.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise ValueError(
            "Usa un URL MCP http/https senza credenziali, query o frammenti."
        )
    path = parsed.path.rstrip("/") or "/mcp"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


async def _validate_destination(url: str) -> None:
    """Block metadata/link-local destinations and clear-text public MCP."""
    parsed = urlsplit(url)
    host = parsed.hostname
    if host is None:
        raise McpClientError("Host MCP non valido.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise McpClientError("Il nome host MCP non può essere risolto.") from exc
    resolved = {
        ipaddress.ip_address(item[4][0].split("%", 1)[0])
        for item in addresses
    }
    if not resolved or any(
        address.is_unspecified
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        for address in resolved
    ):
        raise McpClientError("La destinazione MCP non è consentita.")
    if parsed.scheme == "http" and any(
        not (address.is_private or address.is_loopback)
        for address in resolved
    ):
        raise McpClientError(
            "I server MCP pubblici devono usare HTTPS."
        )


def _server_headers(server: McpServerRecord) -> dict[str, str]:
    headers = {"User-Agent": "OmniProxy-AI-MCP/1"}
    if server.auth_type != "bearer":
        return headers
    if not server.auth_ciphertext:
        raise McpClientError("Il token Bearer MCP non è configurato.")
    try:
        payload = ProviderVault().decrypt_json(server.auth_ciphertext)
    except ProviderVaultError as exc:
        raise McpClientError("Le credenziali MCP non sono leggibili.") from exc
    token = payload.get("bearer_token")
    if not isinstance(token, str) or not token:
        raise McpClientError("Il token Bearer MCP non è valido.")
    headers["Authorization"] = f"Bearer {token}"
    return headers


def _model_json(value: object) -> dict[str, object] | None:
    model_dump = getattr(value, "model_dump", None)
    if not callable(model_dump):
        return None
    result = model_dump(mode="json", by_alias=True, exclude_none=True)
    return result if isinstance(result, dict) else None


def _safe_tool(tool: object) -> dict[str, object] | None:
    name = getattr(tool, "name", None)
    input_schema = getattr(tool, "input_schema", None)
    if not isinstance(name, str) or not isinstance(input_schema, dict):
        return None
    description = getattr(tool, "description", None) or ""
    if not isinstance(description, str):
        description = ""
    # La discovery non deve consentire a un server remoto di riempire il DB o
    # il contesto LLM con schemi arbitrariamente grandi.
    try:
        schema_size = len(
            json.dumps(input_schema, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError):
        return None
    if schema_size > 50_000:
        return None
    result: dict[str, object] = {
        "name": name,
        "title": getattr(tool, "title", None) or name,
        "description": description[:4_000],
        "input_schema": input_schema,
    }
    annotations = _model_json(getattr(tool, "annotations", None))
    if annotations:
        result["annotations"] = annotations
    return result


class McpGatewayClient:
    """Short-lived official MCP client used for discovery and tool calls."""

    async def discover(self, server: McpServerRecord) -> McpDiscovery:
        url = normalize_mcp_url(server.url)
        await _validate_destination(url)
        async with asyncio.timeout(settings.mcp_call_timeout_seconds):
            async with self._client(server, url) as client:
                tools: list[dict[str, object]] = []
                cursor: str | None = None
                while True:
                    result = await client.list_tools(cursor=cursor)
                    tools.extend(
                        safe
                        for item in result.tools
                        if (safe := _safe_tool(item)) is not None
                    )
                    cursor = getattr(result, "next_cursor", None)
                    if cursor is None:
                        break
                    if len(tools) > 1_000:
                        raise McpClientError(
                            "Il server MCP espone troppi strumenti."
                        )
                info = _model_json(client.server_info)
                return McpDiscovery(
                    protocol_version=(
                        str(client.protocol_version)
                        if client.protocol_version is not None
                        else None
                    ),
                    server_info=info,
                    tools=tools,
                )

    async def call_tool(
        self,
        server: McpServerRecord,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        url = normalize_mcp_url(server.url)
        await _validate_destination(url)
        async with asyncio.timeout(settings.mcp_call_timeout_seconds):
            async with self._client(server, url) as client:
                result = await client.call_tool(tool_name, arguments)
        rendered = self._render_result(result)
        if bool(getattr(result, "is_error", False)):
            raise McpToolError(rendered[:2_000] or "Il tool MCP ha fallito.")
        return rendered

    @staticmethod
    def _client(server: McpServerRecord, url: str):
        timeout = httpx2.Timeout(
            settings.mcp_call_timeout_seconds,
            connect=settings.mcp_connect_timeout_seconds,
        )

        class _Connection:
            async def __aenter__(self):
                self.http = httpx2.AsyncClient(
                    headers=_server_headers(server),
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                )
                await self.http.__aenter__()
                self.transport = streamable_http_client(
                    url,
                    http_client=self.http,
                )
                self.client = Client(self.transport)
                try:
                    return await self.client.__aenter__()
                except BaseException:
                    await self.http.__aexit__(None, None, None)
                    raise

            async def __aexit__(self, exc_type, exc, traceback):
                try:
                    await self.client.__aexit__(exc_type, exc, traceback)
                finally:
                    await self.http.__aexit__(exc_type, exc, traceback)

        return _Connection()

    @staticmethod
    def _render_result(result: object) -> str:
        parts: list[str] = []
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            parts.append(
                json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
            )
        for block in getattr(result, "content", ()) or ():
            text = getattr(block, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
        rendered = "\n".join(dict.fromkeys(parts)).strip()
        if not rendered:
            rendered = "[Il tool MCP non ha restituito contenuto testuale.]"
        limit = settings.mcp_max_result_characters
        if len(rendered) > limit:
            rendered = f"{rendered[:limit]}\n[Risultato MCP abbreviato da OmniProxy]"
        return rendered
