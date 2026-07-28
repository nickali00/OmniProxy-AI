from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.providers.ollama import OllamaProvider


ADMIN_KEY = "sk-local-admin-test-key-with-at-least-thirty-two-characters"


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
                    "reasoning_efforts": ["off", "on"],
                    "default_reasoning_effort": "off",
                }
            ],
        },
        {
            "id": "codex",
            "name": "Codex",
            "connected": False,
            "models": [],
        },
        {
            "id": "gemini",
            "name": "Gemini",
            "connected": False,
            "models": [],
        },
        {
            "id": "claude",
            "name": "Claude",
            "connected": False,
            "models": [],
        },
    ]


def test_managed_api_crud_and_fixed_routing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", ADMIN_KEY)

    async def fake_catalog(_app):
        return _catalog()

    async def fake_require_ollama_client(_app):
        return object()

    observed: dict[str, object] = {}

    async def fake_complete(self, request, resolved_model):
        observed["resolved_model"] = resolved_model
        observed["think"] = self._think
        return "profilo vincolato"

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    monkeypatch.setattr(
        "app.main._require_ollama_client",
        fake_require_ollama_client,
    )
    monkeypatch.setattr(OllamaProvider, "complete", fake_complete)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/gateway-apis",
            headers=_dashboard_headers(),
            json={
                "name": "Assistente contabilità",
                "provider": "ollama",
                "model": "qwen-test:latest",
                "reasoning_effort": "on",
            },
        )
        assert created.status_code == 201
        secret = created.json()["api_key"]
        api_id = created.json()["id"]
        slug = created.json()["slug"]

        listed = client.get("/api/gateway-apis")
        assert listed.status_code == 200
        assert listed.json()["data"][0]["slug"] == slug
        assert secret not in listed.text
        managed = listed.json()["data"][0]
        assert managed["status"] == "active"

        paused = client.patch(
            f"/api/keys/{managed['api_key_id']}/toggle",
            headers=_dashboard_headers(),
        )
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"
        assert client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {secret}"},
        ).status_code == 401

        resumed = client.patch(
            f"/api/keys/{managed['api_key_id']}/toggle",
            headers=_dashboard_headers(),
        )
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "active"

        renamed = client.patch(
            f"/api/gateway-apis/{api_id}",
            headers=_dashboard_headers(),
            json={
                "name": "Contabilità principale",
                "provider": "ollama",
                "model": "qwen-test:latest",
                "reasoning_effort": "on",
            },
        )
        assert renamed.status_code == 200
        assert renamed.json()["slug"] == slug

        models = client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert models.status_code == 200
        assert [item["id"] for item in models.json()["data"]] == [slug]

        completion = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                # Il payload prova a cambiare modello, ma la chiave deve
                # continuare a usare il routing persistito.
                "model": "modello-che-il-client-prova-a-forzare",
                "messages": [{"role": "user", "content": "Ciao"}],
            },
        )
        assert completion.status_code == 200
        assert completion.json()["model"] == slug
        assert observed == {
            "resolved_model": "qwen-test:latest",
            "think": True,
        }

        deleted = client.delete(
            f"/api/gateway-apis/{api_id}",
            headers=_dashboard_headers(),
        )
        assert deleted.status_code == 200
        assert client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {secret}"},
        ).status_code == 401


def test_managed_api_key_lifecycle_blocks_paused_and_revoked_keys(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        settings,
        "database_path",
        tmp_path / "gateway.sqlite3",
    )
    monkeypatch.setattr(settings, "bootstrap_api_key", ADMIN_KEY)

    async def fake_catalog(_app):
        return _catalog()

    async def fake_require_ollama_client(_app):
        return object()

    provider_calls = 0

    async def fake_complete(self, request, resolved_model):
        nonlocal provider_calls
        provider_calls += 1
        assert resolved_model == "qwen-test:latest"
        return "risposta gestita"

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    monkeypatch.setattr(
        "app.main._require_ollama_client",
        fake_require_ollama_client,
    )
    monkeypatch.setattr(OllamaProvider, "complete", fake_complete)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/gateway-apis",
            headers=_dashboard_headers(),
            json={
                "name": "API ciclo chiave",
                "provider": "ollama",
                "model": "qwen-test:latest",
                "reasoning_effort": "off",
            },
        )
        assert created.status_code == 201
        secret = created.json()["api_key"]
        api_id = created.json()["id"]
        key_id = created.json()["api_key_id"]
        slug = created.json()["slug"]
        auth_headers = {"Authorization": f"Bearer {secret}"}
        completion_payload = {
            "model": slug,
            "messages": [
                {"role": "user", "content": "Verifica stato"}
            ],
        }

        active_models = client.get("/v1/models", headers=auth_headers)
        active_completion = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json=completion_payload,
        )
        assert active_models.status_code == 200
        assert [
            item["id"] for item in active_models.json()["data"]
        ] == [slug]
        assert active_completion.status_code == 200
        assert provider_calls == 1

        paused = client.patch(
            f"/api/keys/{key_id}/toggle",
            headers=_dashboard_headers(),
        )
        paused_models = client.get("/v1/models", headers=auth_headers)
        paused_completion = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json=completion_payload,
        )
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"
        assert paused_models.status_code == 401
        assert paused_completion.status_code == 401
        assert paused_models.json()["error"]["code"] == "invalid_api_key"
        assert (
            paused_completion.json()["error"]["code"]
            == "invalid_api_key"
        )
        assert provider_calls == 1

        resumed = client.patch(
            f"/api/keys/{key_id}/toggle",
            headers=_dashboard_headers(),
        )
        resumed_completion = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json=completion_payload,
        )
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "active"
        assert resumed_completion.status_code == 200
        assert provider_calls == 2

        revoked = client.delete(
            f"/api/gateway-apis/{api_id}",
            headers=_dashboard_headers(),
        )
        revoked_models = client.get("/v1/models", headers=auth_headers)
        revoked_completion = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json=completion_payload,
        )
        assert revoked.status_code == 200
        assert revoked_models.status_code == 401
        assert revoked_completion.status_code == 401
        assert revoked_models.json()["error"]["code"] == "invalid_api_key"
        assert (
            revoked_completion.json()["error"]["code"]
            == "invalid_api_key"
        )
        assert provider_calls == 2


def test_cannot_create_api_for_disconnected_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", ADMIN_KEY)

    async def fake_catalog(_app):
        return _catalog()

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/gateway-apis",
            headers=_dashboard_headers(),
            json={
                "name": "Codex non collegato",
                "provider": "codex",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
            },
        )

    assert response.status_code == 409
    assert response.json()["error"] == "provider_not_connected"
