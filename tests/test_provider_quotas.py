import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.provider_broker import CodexBrokerClient


class FakeQuotaBroker:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def quota(self) -> dict[str, object]:
        return self.payload


def test_provider_quota_dashboard_preserves_official_availability(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")

    async def fake_discovery(_app, *, timeout):
        assert timeout == 2.0
        return object(), {"models": [{"name": settings.ollama_model}]}

    monkeypatch.setattr("app.main._discover_ollama", fake_discovery)
    monkeypatch.setattr(
        "app.main.CodexBrokerClient",
        lambda _client: FakeQuotaBroker(
            {
                "provider": "codex",
                "connected": True,
                "available": True,
                "unlimited": False,
                "source": "codex_app_server",
                "remaining_percent": 99,
                "windows": [
                    {
                        "id": "codex:primary",
                        "remaining_percent": 99,
                        "used_percent": 1,
                        "window_minutes": 300,
                        "resets_at": 1_900_000_000,
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(
        "app.main.AntigravityBrokerClient",
        lambda _client: FakeQuotaBroker(
            {
                "provider": "gemini",
                "connected": True,
                "available": False,
                "unlimited": False,
                "source": "antigravity_cli",
                "reason": "interactive_only",
                "windows": [],
            }
        ),
    )
    monkeypatch.setattr(
        "app.main.ClaudeBrokerClient",
        lambda _client: FakeQuotaBroker(
            {
                "provider": "claude",
                "connected": False,
                "available": False,
                "unlimited": False,
                "source": "claude_code",
                "reason": "not_connected",
                "windows": [],
            }
        ),
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/api/providers/quotas")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    providers = {
        row["provider"]: row for row in response.json()["providers"]
    }
    assert list(providers) == ["ollama", "codex", "gemini", "claude"]
    assert providers["ollama"]["unlimited"] is True
    assert providers["ollama"]["remaining_percent"] == 100
    assert providers["codex"]["remaining_percent"] == 99
    assert providers["codex"]["windows"][0]["window_minutes"] == 300
    assert providers["gemini"]["reason"] == "interactive_only"
    assert providers["claude"]["reason"] == "not_connected"
    assert "token" not in response.text.lower()
    assert "email" not in response.text.lower()


@pytest.mark.asyncio
async def test_broker_quota_uses_internal_no_store_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/quota"
        return httpx.Response(
            200,
            json={
                "provider": "codex",
                "available": True,
                "remaining_percent": 99,
                "windows": [],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://broker.test",
    ) as http_client:
        payload = await CodexBrokerClient(http_client).quota()

    assert payload["remaining_percent"] == 99
