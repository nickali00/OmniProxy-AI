import json
from functools import partial
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.database import create_gateway_api
from app.main import app
from scripts.export_openapi import serialized


def test_discovery_is_key_scoped_and_contains_no_backend_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "admin_auth_required", False)
    monkeypatch.setattr(settings, "otel_enabled", False)
    monkeypatch.setattr(settings, "database_path", tmp_path / "contract.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", "sk-local-contract-synthetic-at-least-thirty-two-characters")
    monkeypatch.setattr(settings, "audio_backend_url", "http://private-backend.example/secret")
    monkeypatch.setattr(settings, "audio_api_key", "PRIVATE_CREDENTIAL")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        missing = client.get("/v1/capabilities")
        assert missing.status_code == 401
        assert missing.headers["X-OmniProxy-Contract-Version"] == "1"
        profile, key = client.portal.call(partial(create_gateway_api, name="Scoped", provider="codex", model="test-model", reasoning_effort="low", limits={"input_tokens": 100, "output_tokens": 20}))
        body = client.get("/v1/capabilities", headers={"Authorization": "Bearer " + key})
        result = body.json()
        assert result["models"] == [profile.slug]
        assert result["limits"]["input_tokens"] == 100 and result["limits"]["context_tokens"] is None
        assert result["tools"]["client_functions"] and not result["tools"]["codex_images_with_client_functions"]
        assert result["features"]["repo_patch_jobs"] and result["features"]["audio_transcription_configured"]
        assert result["availability"] == "configuration_only_not_health"
        assert body.headers["Cache-Control"] == "no-store"
        text = json.dumps(result)
        assert "PRIVATE_CREDENTIAL" not in text and "private-backend" not in text and key not in text


def test_published_schema_matches_routes_and_auth_contract():
    text = serialized()
    assert Path("spec/openapi.json").read_text() == text
    schema = json.loads(text)
    assert all(path.startswith(("/v1/", "/v1beta/", "/admin/v1/")) for path in schema["paths"])
    assert schema["paths"]["/v1/messages"]["post"]["security"] == [{"ApplicationBearer": []}, {"AnthropicKey": []}]
    assert schema["paths"]["/v1/chat/completions"]["post"]["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/GatewayError")
    assert schema["x-omniproxy-contract-version"] == "1"
