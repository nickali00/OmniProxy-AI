import json

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


KEY = "sk-local-test-key-with-at-least-thirty-two-characters"


def test_gemini_native_text_and_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)
    monkeypatch.setattr(settings, "external_mock_latency_seconds", 0)
    payload = {"contents": [{"role": "user", "parts": [{"text": "Ciao"}]}], "generationConfig": {"maxOutputTokens": 64}}
    headers = {"x-goog-api-key": KEY}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/v1beta/models/reasoning-avanzato:generateContent", json=payload).status_code == 401
        response = client.post("/v1beta/models/reasoning-avanzato:generateContent", headers=headers, json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["candidates"][0]["content"]["parts"][0]["text"]
        assert body["candidates"][0]["finishReason"] == "STOP"
        assert body["usageMetadata"]["totalTokenCount"] > 0
        streamed = client.post("/v1beta/models/reasoning-avanzato:streamGenerateContent?alt=sse", headers=headers, json=payload)
        assert streamed.status_code == 200
        chunks = [json.loads(line[6:]) for line in streamed.text.splitlines() if line.startswith("data: ")]
        assert len(chunks) > 1
        assert chunks[-1]["candidates"][0]["finishReason"] == "STOP"
        assert chunks[-1]["usageMetadata"]["totalTokenCount"] > 0


def test_gemini_native_rejects_unverified_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)
    marker = "PRIVATE_GEMINI_MARKER_91"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/v1beta/models/local:generateContent", headers={"x-goog-api-key": KEY}, json={"contents": [{"parts": [{"inlineData": {"data": marker, "mimeType": "image/png"}}]}]})
        assert response.status_code == 400
        assert marker not in response.text
