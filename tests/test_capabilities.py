import sqlite3

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.mcp_client import McpDiscovery, McpGatewayClient
from app.providers.ollama import OllamaProvider

ADMIN_KEY = "sk-local-capabilities-test-key-with-thirty-two-characters"


def _dashboard_headers() -> dict[str, str]:
    return {
        "Origin": "http://127.0.0.1",
        "X-OmniProxy-Request": "dashboard",
    }


def _catalog() -> list[dict[str, object]]:
    return [
        {
            "id": "ollama",
            "name": "Ollama",
            "connected": True,
            "models": [
                {
                    "id": "qwen-test:latest",
                    "display_name": "Qwen Test",
                    "description": "Fixture locale",
                    "is_default": True,
                    "reasoning_efforts": ["off"],
                    "default_reasoning_effort": "off",
                }
            ],
        },
        {"id": "codex", "name": "Codex", "connected": False, "models": []},
        {"id": "gemini", "name": "Gemini", "connected": False, "models": []},
        {"id": "claude", "name": "Claude", "connected": False, "models": []},
    ]


def test_skill_is_attached_to_managed_api_and_injected(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", ADMIN_KEY)

    async def fake_catalog(_app):
        return _catalog()

    async def fake_require_ollama_client(_app):
        return object()

    observed: dict[str, str] = {}

    async def fake_complete(self, request, resolved_model):
        observed["system"] = request.messages[0].text_content()
        observed["model"] = resolved_model
        return "risposta con skill"

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    monkeypatch.setattr(
        "app.main._require_ollama_client",
        fake_require_ollama_client,
    )
    monkeypatch.setattr(OllamaProvider, "complete", fake_complete)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        skill = client.post(
            "/api/skills",
            headers=_dashboard_headers(),
            json={
                "name": "Risposte concise",
                "description": "Risposte brevi",
                "instructions": "Rispondi in massimo tre punti chiari.",
            },
        )
        assert skill.status_code == 201
        skill_id = skill.json()["id"]

        created = client.post(
            "/api/gateway-apis",
            headers=_dashboard_headers(),
            json={
                "name": "API con Skill",
                "provider": "ollama",
                "model": "qwen-test:latest",
                "reasoning_effort": "off",
                "skill_ids": [skill_id],
            },
        )
        assert created.status_code == 201
        assert created.json()["skill_ids"] == [skill_id]
        secret = created.json()["api_key"]

        completion = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": created.json()["slug"],
                "messages": [{"role": "user", "content": "Spiega"}],
            },
        )
        assert completion.status_code == 200
        assert completion.json()["choices"][0]["message"]["content"] == (
            "risposta con skill"
        )
        assert "Rispondi in massimo tre punti chiari." in observed["system"]
        assert observed["model"] == "qwen-test:latest"


def test_allowlisted_mcp_tool_chain_and_private_audit(tmp_path, monkeypatch):
    database_path = tmp_path / "gateway.sqlite3"
    monkeypatch.setattr(settings, "database_path", database_path)
    monkeypatch.setattr(settings, "bootstrap_api_key", ADMIN_KEY)

    async def fake_catalog(_app):
        return _catalog()

    async def fake_require_ollama_client(_app):
        return object()

    async def fake_discover(self, server):
        return McpDiscovery(
            protocol_version="2026-07-28",
            server_info={"name": "Casa", "version": "1.0"},
            tools=[
                {
                    "name": "temperature",
                    "title": "Temperatura",
                    "description": "Legge la temperatura di una stanza.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"room": {"type": "string"}},
                        "required": ["room"],
                    },
                }
            ],
        )

    async def fake_tool(self, server, tool_name, arguments):
        assert server.name == "Home Assistant MCP"
        assert tool_name == "temperature"
        assert arguments == {"room": "studio"}
        return "22 °C"

    calls = 0

    async def fake_complete(self, request, resolved_model):
        nonlocal calls
        calls += 1
        if calls <= 2:
            assert "home-assistant-mcp.temperature" in (
                request.messages[0].text_content()
            )
            return (
                '{"omniproxy_mcp_call":{"tool":'
                '"home-assistant-mcp.temperature",'
                '"arguments":{"room":"studio"}}}'
            )
        assert "22 °C" in request.messages[-1].text_content()
        return "Nello studio ci sono 22 °C."

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    monkeypatch.setattr(
        "app.main._require_ollama_client",
        fake_require_ollama_client,
    )
    monkeypatch.setattr(McpGatewayClient, "discover", fake_discover)
    monkeypatch.setattr(McpGatewayClient, "call_tool", fake_tool)
    monkeypatch.setattr(OllamaProvider, "complete", fake_complete)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        server = client.post(
            "/api/mcp/servers",
            headers=_dashboard_headers(),
            json={
                "name": "Home Assistant MCP",
                "url": "http://mcp-server:3000/mcp",
                "auth_type": "bearer",
                "bearer_token": "secret-token-that-must-never-be-listed",
            },
        )
        assert server.status_code == 201
        server_body = server.json()
        assert server_body["status"] == "connected"
        assert server_body["has_credentials"] is True
        assert "secret-token" not in server.text

        created = client.post(
            "/api/gateway-apis",
            headers=_dashboard_headers(),
            json={
                "name": "API MCP casa",
                "provider": "ollama",
                "model": "qwen-test:latest",
                "reasoning_effort": "off",
                "mcp_tools": [
                    {
                        "server_id": server_body["id"],
                        "tool_names": ["temperature"],
                    }
                ],
            },
        )
        assert created.status_code == 201
        secret = created.json()["api_key"]

        completion = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": created.json()["slug"],
                "messages": [
                    {"role": "user", "content": "Temperatura studio?"}
                ],
            },
        )
        assert completion.status_code == 200
        assert completion.json()["choices"][0]["message"]["content"] == (
            "Nello studio ci sono 22 °C."
        )
        assert completion.json()["usage"]["prompt_tokens"] > 0

    connection = sqlite3.connect(database_path)
    audit = connection.execute(
        """
        SELECT tool_name, status, error_code
        FROM mcp_tool_usage
        """
    ).fetchone()
    audit_count = connection.execute("SELECT count(*) FROM mcp_tool_usage").fetchone()[0]
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(mcp_tool_usage)")
    }
    connection.close()
    assert audit == ("temperature", "success", None)
    assert audit_count == 1 and calls == 3
    assert "arguments" not in columns
    assert "result" not in columns
