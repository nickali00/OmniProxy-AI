from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


API_KEY = "sk-local-usage-test-key-with-at-least-thirty-two-characters"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"}


def test_usage_dashboard_aggregates_and_filters_local_logs(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", API_KEY)
    monkeypatch.setattr(settings, "external_mock_latency_seconds", 0)
    private_prompt = "TESTO-PRIVATO-NON-DEVE-APPARIRE"

    with TestClient(app, base_url="http://127.0.0.1") as client:
        successful = client.post(
            "/v1/chat/completions",
            headers=_headers(),
            json={
                "model": "reasoning-avanzato",
                "messages": [{"role": "user", "content": private_prompt}],
            },
        )
        failed = client.post(
            "/v1/chat/completions",
            headers=_headers(),
            json={
                "model": "modello-inesistente",
                "messages": [{"role": "user", "content": private_prompt}],
            },
        )
        usage = client.get("/api/usage?period=7d&limit=10")
        external_only = client.get(
            "/api/usage?period=7d&provider=external_mock"
        )
        invalid = client.get("/api/usage?period=mai")

    assert successful.status_code == 200
    assert failed.status_code == 404
    assert usage.status_code == 200
    body = usage.json()
    assert body["summary"]["request_count"] == 2
    assert body["summary"]["successful_requests"] == 1
    assert body["summary"]["failed_requests"] == 1
    assert body["summary"]["total_tokens"] > 0
    assert {row["provider"] for row in body["by_provider"]} == {
        "external_mock",
        "unresolved",
    }
    assert len(body["requests"]) == 2
    assert body["filters"]["apis"][0]["api_name"] == "bootstrap"
    assert private_prompt not in usage.text
    assert API_KEY not in usage.text
    assert external_only.json()["summary"]["request_count"] == 1
    assert invalid.status_code == 400
    assert invalid.json()["error"] == "invalid_usage_period"
