import sqlite3

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.providers.ollama import OllamaProvider


API_KEY = "sk-local-test-key-with-at-least-thirty-two-characters"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"}


def _dashboard_headers() -> dict[str, str]:
    return {
        "Origin": "http://127.0.0.1",
        "X-OmniProxy-Request": "dashboard",
    }


def test_authentication_and_cloud_usage(tmp_path, monkeypatch):
    database_path = tmp_path / "gateway.sqlite3"
    monkeypatch.setattr(settings, "database_path", database_path)
    monkeypatch.setattr(settings, "bootstrap_api_key", API_KEY)
    monkeypatch.setattr(settings, "external_mock_latency_seconds", 0)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        missing = client.get("/v1/models")
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "invalid_api_key"

        response = client.post(
            "/v1/chat/completions",
            headers=_headers(),
            json={
                "model": "reasoning-avanzato",
                "messages": [{"role": "user", "content": "Spiega il routing"}],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "chat.completion"
        assert body["model"] == "reasoning-avanzato"
        assert body["usage"]["total_tokens"] > 0

    connection = sqlite3.connect(database_path)
    log = connection.execute(
        """
        SELECT requested_model, routed_provider, prompt_tokens,
               completion_tokens, total_tokens, status_code
        FROM usage_logs
        """
    ).fetchone()
    connection.close()

    assert log[0:2] == ("reasoning-avanzato", "external_mock")
    assert log[2] > 0
    assert log[3] > 0
    assert log[4] == log[2] + log[3]
    assert log[5] == 200


def test_local_model_routing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", API_KEY)

    async def fake_complete(self, request, resolved_model):
        assert resolved_model == settings.ollama_model
        return "risposta locale"

    async def fake_require_ollama_client(_app):
        return object()

    monkeypatch.setattr(OllamaProvider, "complete", fake_complete)
    monkeypatch.setattr(
        "app.main._require_ollama_client",
        fake_require_ollama_client,
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/v1/chat/completions",
            headers=_headers(),
            json={
                "model": "local",
                "messages": [{"role": "user", "content": "Ciao"}],
            },
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "risposta locale"


def test_streaming_is_openai_compatible(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", API_KEY)
    monkeypatch.setattr(settings, "external_mock_latency_seconds", 0)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/v1/chat/completions",
            headers=_headers(),
            json={
                "model": "reasoning-avanzato",
                "messages": [{"role": "user", "content": "Ciao"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )

    assert response.status_code == 200
    assert "chat.completion.chunk" in response.text
    assert '"usage": {' in response.text
    assert response.text.endswith("data: [DONE]\n\n")


def test_unknown_model_is_logged(tmp_path, monkeypatch):
    database_path = tmp_path / "gateway.sqlite3"
    monkeypatch.setattr(settings, "database_path", database_path)
    monkeypatch.setattr(settings, "bootstrap_api_key", API_KEY)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/v1/chat/completions",
            headers=_headers(),
            json={
                "model": "provider-inventato",
                "messages": [{"role": "user", "content": "Ciao"}],
            },
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    connection = sqlite3.connect(database_path)
    status_code, error_code = connection.execute(
        "SELECT status_code, error_code FROM usage_logs"
    ).fetchone()
    connection.close()
    assert (status_code, error_code) == (404, "model_not_found")


def test_bootstrap_key_rotation_revokes_previous_secret(tmp_path, monkeypatch):
    database_path = tmp_path / "gateway.sqlite3"
    old_key = "sk-local-old-bootstrap-key-with-thirty-two-characters"
    new_key = "sk-local-new-bootstrap-key-with-thirty-two-characters"
    monkeypatch.setattr(settings, "database_path", database_path)
    monkeypatch.setattr(settings, "bootstrap_api_key", old_key)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {old_key}"},
        ).status_code == 200

    monkeypatch.setattr(settings, "bootstrap_api_key", new_key)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {old_key}"},
        ).status_code == 401
        assert client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {new_key}"},
        ).status_code == 200


def test_api_key_toggle_pauses_and_resumes_auth(tmp_path, monkeypatch):
    database_path = tmp_path / "gateway.sqlite3"
    monkeypatch.setattr(settings, "database_path", database_path)
    monkeypatch.setattr(settings, "bootstrap_api_key", API_KEY)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        connection = sqlite3.connect(database_path)
        row = connection.execute(
            "SELECT id FROM api_keys WHERE name = 'bootstrap'"
        ).fetchone()
        connection.close()
        assert row is not None
        key_id = int(row[0])

        paused = client.patch(
            f"/api/keys/{key_id}/toggle",
            headers=_dashboard_headers(),
        )
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"
        paused_request = client.get(
            "/v1/models",
            headers=_headers(),
        )
        paused_completion = client.post(
            "/v1/chat/completions",
            headers=_headers(),
            json={
                "model": "reasoning-avanzato",
                "messages": [
                    {"role": "user", "content": "Non eseguire"}
                ],
            },
        )
        assert paused_request.status_code == 401
        assert paused_request.json()["error"]["code"] == "invalid_api_key"
        assert paused_completion.status_code == 401
        assert (
            paused_completion.json()["error"]["code"]
            == "invalid_api_key"
        )

        connection = sqlite3.connect(database_path)
        stored_status = connection.execute(
            "SELECT status FROM api_keys WHERE id = ?",
            (key_id,),
        ).fetchone()[0]
        connection.close()
        assert stored_status == "paused"

        resumed = client.patch(
            f"/api/keys/{key_id}/toggle",
            headers=_dashboard_headers(),
        )
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "active"
        assert client.get(
            "/v1/models",
            headers=_headers(),
        ).status_code == 200
