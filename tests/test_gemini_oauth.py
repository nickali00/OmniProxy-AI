import os

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.provider_vault import ProviderVault


ATTEMPT_ID = "a497024c-5d97-47cd-a8b5-3b609d84fe92"
AUTH_URL = (
    "https://accounts.google.com/o/oauth2/auth"
    "?redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback"
    "&response_type=code"
    "&code_challenge_method=S256"
    "&code_challenge=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"
    "&state=antigravity-opaque-state"
)


def dashboard_headers() -> dict[str, str]:
    return {
        "Origin": "http://127.0.0.1",
        "X-OmniProxy-Request": "dashboard",
    }


class FakeAntigravityBroker:
    def __init__(self) -> None:
        self.submitted_code: str | None = None
        self.cancelled_attempt: str | None = None
        self.disconnected = False

    async def status(self) -> dict[str, object]:
        return {
            "provider": "gemini",
            "installed": True,
            "connected": False,
            "auth_method": "none",
            "client_mode": "official_headless_cli",
            "models": [],
            "attempt": None,
        }

    async def start_auth(self) -> dict[str, object]:
        return {
            "provider": "gemini",
            "installed": True,
            "connected": False,
            "auth_url": AUTH_URL,
            "attempt": {
                "id": ATTEMPT_ID,
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
            "provider": "gemini",
            "connected": False,
            "attempt": {
                "id": attempt_id,
                "state": "verifying",
                "expires_at": "2030-01-01T00:00:00.000Z",
                "requires_code": False,
            },
        }

    async def cancel_auth(self, attempt_id: str) -> dict[str, object]:
        self.cancelled_attempt = attempt_id
        return {"status": "cancelled"}

    async def disconnect(self) -> dict[str, object]:
        self.disconnected = True
        return {"provider": "gemini", "connected": False}


def test_gemini_login_uses_antigravity_and_requires_same_origin(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    fake = FakeAntigravityBroker()
    monkeypatch.setattr(
        "app.main.AntigravityBrokerClient",
        lambda _client: fake,
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        rejected = client.post("/api/providers/gemini/auth/start")
        accepted = client.post(
            "/api/providers/gemini/auth/start",
            headers=dashboard_headers(),
        )

    assert rejected.status_code == 403
    assert accepted.status_code == 201
    assert accepted.json()["auth_url"] == AUTH_URL
    assert accepted.json()["attempt"]["requires_code"] is True
    assert accepted.headers["cache-control"] == "no-store"
    assert "token" not in accepted.text.lower()


def test_gemini_code_is_ephemeral_and_session_can_be_removed(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    fake = FakeAntigravityBroker()
    monkeypatch.setattr(
        "app.main.AntigravityBrokerClient",
        lambda _client: fake,
    )
    one_time_code = "temporary-google-code-value"

    with TestClient(app, base_url="http://127.0.0.1") as client:
        submitted = client.post(
            f"/api/providers/gemini/auth/{ATTEMPT_ID}/code",
            headers=dashboard_headers(),
            json={"code": one_time_code},
        )
        disconnected = client.delete(
            "/api/providers/gemini/connection",
            headers=dashboard_headers(),
        )

    assert submitted.status_code == 202
    assert fake.submitted_code == one_time_code
    assert one_time_code not in submitted.text
    assert disconnected.status_code == 200
    assert fake.disconnected is True


def test_gemini_auth_attempt_can_be_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    fake = FakeAntigravityBroker()
    monkeypatch.setattr(
        "app.main.AntigravityBrokerClient",
        lambda _client: fake,
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.delete(
            f"/api/providers/gemini/auth/{ATTEMPT_ID}",
            headers=dashboard_headers(),
        )

    assert response.status_code == 200
    assert fake.cancelled_attempt == ATTEMPT_ID


def test_dashboard_describes_simple_official_antigravity_flow(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")

    with TestClient(app, base_url="http://127.0.0.1:8181") as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Google · Antigravity" in response.text
    assert "Login Google ufficiale" in response.text
    assert "client_secret.json" not in response.text
    assert "console.cloud.google.com" not in response.text


def test_provider_vault_migrates_legacy_key_to_separate_path(
    tmp_path,
    monkeypatch,
):
    data_dir = tmp_path / "data"
    vault_dir = tmp_path / "vault"
    data_dir.mkdir()
    legacy_path = data_dir / ".omniproxy-vault.key"
    legacy_key = Fernet.generate_key()
    legacy_path.write_bytes(legacy_key + b"\n")
    os.chmod(legacy_path, 0o600)

    target = vault_dir / "omniproxy-vault.key"
    monkeypatch.setattr(settings, "database_path", data_dir / "gateway.sqlite3")
    monkeypatch.setattr(settings, "provider_vault_key_path", target)

    encrypted = ProviderVault().encrypt_json({"refresh_token": "secret"})

    assert target.read_bytes().strip() == legacy_key
    assert os.stat(target).st_mode & 0o777 == 0o600
    assert ProviderVault().decrypt_json(encrypted) == {
        "refresh_token": "secret"
    }
