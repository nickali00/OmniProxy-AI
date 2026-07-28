import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.provider_broker import (
    AntigravityBrokerClient,
    ClaudeBrokerClient,
    CodexBrokerClient,
    ProviderBrokerError,
)


class FakeClaudeBroker:
    def __init__(self) -> None:
        self.submitted_code: str | None = None

    async def status(self) -> dict[str, object]:
        return {
            "provider": "claude",
            "installed": True,
            "connected": False,
            "auth_method": "none",
            "attempt": None,
        }

    async def start_auth(self) -> dict[str, object]:
        return {
            "provider": "claude",
            "installed": True,
            "connected": False,
            "auth_url": (
                "https://claude.com/cai/oauth/authorize"
                "?code=true&state=opaque"
            ),
            "attempt": {
                "id": "a497024c-5d97-47cd-a8b5-3b609d84fe92",
                "state": "waiting_for_user",
                "expires_at": "2030-01-01T00:00:00.000Z",
                "requires_code": True,
            },
        }

    async def submit_code(
        self,
        attempt_id: str,
        code: str,
    ) -> dict[str, object]:
        self.submitted_code = code
        return {
            "provider": "claude",
            "connected": False,
            "attempt": {
                "id": attempt_id,
                "state": "verifying",
                "expires_at": "2030-01-01T00:00:00.000Z",
                "requires_code": False,
            },
        }

    async def cancel_auth(self, attempt_id: str) -> dict[str, object]:
        return {"status": "cancelled", "attempt_id": attempt_id}


def test_provider_mutations_require_same_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    fake = FakeClaudeBroker()
    monkeypatch.setattr(
        "app.main.ClaudeBrokerClient",
        lambda _client: fake,
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        rejected = client.post("/api/providers/claude/auth/start")
        assert rejected.status_code == 403

        accepted = client.post(
            "/api/providers/claude/auth/start",
            headers={
                "Origin": "http://127.0.0.1",
                "X-OmniProxy-Request": "dashboard",
            },
        )
        assert accepted.status_code == 201
        assert accepted.json()["auth_url"].startswith("https://claude.com/")
        assert accepted.headers["cache-control"] == "no-store"
        assert accepted.headers["x-frame-options"] == "DENY"


def test_one_time_code_is_forwarded_only_to_broker(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    fake = FakeClaudeBroker()
    monkeypatch.setattr(
        "app.main.ClaudeBrokerClient",
        lambda _client: fake,
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            (
                "/api/providers/claude/auth/"
                "a497024c-5d97-47cd-a8b5-3b609d84fe92/code"
            ),
            headers={
                "Origin": "http://127.0.0.1",
                "X-OmniProxy-Request": "dashboard",
            },
            json={"code": "temporary-code-value"},
        )

    assert response.status_code == 202
    assert fake.submitted_code == "temporary-code-value"
    assert "temporary-code-value" not in response.text


def test_invalid_one_time_code_is_never_echoed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    fake = FakeClaudeBroker()
    monkeypatch.setattr(
        "app.main.ClaudeBrokerClient",
        lambda _client: fake,
    )

    secret = "short"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            (
                "/api/providers/claude/auth/"
                "a497024c-5d97-47cd-a8b5-3b609d84fe92/code"
            ),
            headers={
                "Origin": "http://127.0.0.1",
                "X-OmniProxy-Request": "dashboard",
            },
            json={"code": secret},
        )

    assert response.status_code == 400
    assert secret not in response.text
    assert fake.submitted_code is None


@pytest.mark.asyncio
async def test_broker_client_rejects_non_claude_oauth_url():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "auth_url": "https://attacker.example/oauth?state=opaque",
                "attempt": None,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://broker.test",
    ) as http_client:
        with pytest.raises(ProviderBrokerError) as error:
            await ClaudeBrokerClient(http_client).start_auth()

    assert error.value.code == "invalid_provider_auth_url"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("broker_type", "invalid_url"),
    [
        (CodexBrokerClient, "https://attacker.example/codex/device"),
        (
            AntigravityBrokerClient,
            (
                "https://accounts.google.com/o/oauth2/auth"
                "?redirect_uri=https%3A%2F%2Fevil.example%2Fcallback"
                "&response_type=code"
                "&code_challenge_method=S256"
                "&code_challenge=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"
                "&state=antigravity-opaque-state"
            ),
        ),
    ],
)
async def test_all_brokers_reject_untrusted_login_destinations(
    broker_type,
    invalid_url,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"auth_url": invalid_url, "attempt": None},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://broker.test",
    ) as http_client:
        with pytest.raises(ProviderBrokerError) as error:
            await broker_type(http_client).start_auth()

    assert error.value.code == "invalid_provider_auth_url"


@pytest.mark.asyncio
async def test_antigravity_broker_accepts_only_official_pkce_login():
    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        "?redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback"
        "&response_type=code"
        "&code_challenge_method=S256"
        "&code_challenge=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"
        "&state=antigravity-opaque-state"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"auth_url": auth_url, "attempt": None},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://broker.test",
    ) as http_client:
        payload = await AntigravityBrokerClient(http_client).start_auth()

    assert payload["auth_url"] == auth_url


def test_codex_device_login_exposes_only_required_public_fields(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")

    class FakeCodexBroker:
        async def start_auth(self):
            return {
                "provider": "codex",
                "installed": True,
                "connected": False,
                "auth_url": "https://auth.openai.com/codex/device",
                "attempt": {
                    "id": "login_attempt_12345678",
                    "state": "waiting_for_user",
                    "expires_at": "2030-01-01T00:00:00.000Z",
                    "requires_code": False,
                    "user_code": "ABCD-EFGH",
                },
            }

    monkeypatch.setattr(
        "app.main.CodexBrokerClient",
        lambda _client: FakeCodexBroker(),
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/providers/codex/auth/start",
            headers={
                "Origin": "http://127.0.0.1",
                "X-OmniProxy-Request": "dashboard",
            },
        )

    assert response.status_code == 201
    assert response.json()["attempt"]["user_code"] == "ABCD-EFGH"
    assert "token" not in response.text.lower()


def test_ollama_is_discovered_without_login(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")

    async def fake_discovery(_app, *, timeout):
        assert timeout == 2.0
        return object(), {
            "models": [
                {"name": settings.ollama_model},
                {"name": "embedding-local:latest"},
            ]
        }

    monkeypatch.setattr("app.main._discover_ollama", fake_discovery)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/api/providers/ollama/status")

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["auth_method"] == "container_api"
    assert body["configured_model_available"] is True
    assert "auth_url" not in body
