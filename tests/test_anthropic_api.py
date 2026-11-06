import json

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


KEY = "sk-local-test-key-with-at-least-thirty-two-characters"


def test_native_messages_text_and_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)
    monkeypatch.setattr(settings, "external_mock_latency_seconds", 0)
    body = {"model": "reasoning-avanzato", "max_tokens": 64, "system": "Rispondi in italiano", "messages": [{"role": "user", "content": "Ciao"}]}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/v1/messages", json=body).status_code == 401
        response = client.post("/v1/messages", headers={"x-api-key": KEY}, json=body)
        assert response.status_code == 200
        message = response.json()
        assert message["type"] == "message" and message["role"] == "assistant"
        assert message["content"][0]["type"] == "text"
        assert message["usage"]["input_tokens"] > 0
        streamed = client.post("/v1/messages", headers={"x-api-key": KEY}, json={**body, "stream": True})
        assert streamed.status_code == 200
        kinds = [line[7:] for line in streamed.text.splitlines() if line.startswith("event: ")]
        assert kinds[:2] == ["message_start", "content_block_start"]
        assert kinds[-2:] == ["message_delta", "message_stop"]
        assert "content_block_delta" in kinds
        for line in streamed.text.splitlines():
            if line.startswith("data: "):
                assert json.loads(line[6:])["type"] in kinds


def test_native_messages_reject_unverified_content(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)
    marker = "PRIVATE_NATIVE_MARKER_23"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/v1/messages", headers={"x-api-key": KEY}, json={"model": "local", "max_tokens": 32, "messages": [{"role": "user", "content": [{"type": "image", "data": marker}]}]})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "native_content_not_supported"
        assert marker not in response.text
