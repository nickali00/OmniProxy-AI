import io
import json
import wave

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import _connect
from app.main import app
from app.token_counter import count_text_tokens

KEY = "sk-local-speech-test-key-with-thirty-two-characters"
HEADERS = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "speech.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)
    with TestClient(app, base_url="http://127.0.0.1") as instance:
        yield instance


async def policy(limits):
    async with _connect() as db:
        await db.execute("UPDATE api_keys SET limits_json=?", (json.dumps(limits),))
        await db.commit()


@pytest.mark.parametrize("format,signature", [("wav", b"RIFF"), ("mp3", b"ID3"), ("flac", b"fLaC"), ("opus", b"OggS"), ("aac", b"\xff"), ("pcm", b"")])
def test_real_local_speech_formats_and_accounting(client, format, signature):
    response = client.post("/v1/audio/speech", headers=HEADERS, json={"model": "speech-local", "input": "Il colore è blu.", "voice": "it", "response_format": format})
    assert response.status_code == 200, response.text
    assert response.content.startswith(signature) and len(response.content) > 100
    assert response.headers["X-OmniProxy-Output-Truncated"] == "false"
    usage = client.get("/v1/usage", headers=HEADERS).json()["usage"]
    assert usage["used_tokens"] == int(response.headers["X-OmniProxy-Input-Tokens"]) + int(response.headers["X-OmniProxy-Output-Tokens"])
    assert usage["reserved_tokens"] == 0


def test_output_limit_trims_valid_wave_and_daily_budget_is_enforced(client):
    text = "This sentence is deliberately long enough to consume the available budget."
    budget = count_text_tokens(text) + 10
    client.portal.call(policy, {"output_tokens": 10, "token_budget": budget, "budget_period": "daily"})
    response = client.post("/v1/audio/speech", headers=HEADERS, json={"input": text, "voice": "en-us", "response_format": "wav"})
    assert response.status_code == 200, response.text
    assert response.headers["X-OmniProxy-Output-Truncated"] == "true"
    assert int(response.headers["X-OmniProxy-Output-Tokens"]) <= 10
    with wave.open(io.BytesIO(response.content)) as audio:
        assert 0 < audio.getnframes() / audio.getframerate() <= 0.201
    second = client.post("/v1/audio/speech", headers=HEADERS, json={"input": text})
    assert second.status_code == 429 and "Retry-After" in second.headers
    assert client.get("/v1/usage", headers=HEADERS).json()["usage"]["reserved_tokens"] == 0


def test_speech_input_and_missing_runtime_fail_before_generation(client, monkeypatch):
    client.portal.call(policy, {"input_tokens": 1})
    response = client.post("/v1/audio/speech", headers=HEADERS, json={"input": "More than one token is used here."})
    assert response.status_code == 400 and response.json()["error"]["code"] == "input_token_limit_exceeded"
    monkeypatch.setattr("app.speech_api.shutil.which", lambda _: None)
    unavailable = client.post("/v1/audio/speech", headers=HEADERS, json={"input": "ciao"})
    assert unavailable.status_code == 503 and unavailable.json()["error"]["code"] == "speech_runtime_missing"
    assert client.get("/v1/usage", headers=HEADERS).json()["usage"]["used_tokens"] == 0


def test_speech_options_are_validated_without_echoing_content(client):
    for body in ({"input": "PRIVATE", "voice": "../private"}, {"input": "PRIVATE", "instructions": "PRIVATE"}, {"input": "   "}, {"input": "\ud800"}):
        response = client.post("/v1/audio/speech", headers={**HEADERS, "Content-Type": "application/json"}, content=json.dumps(body).encode())
        assert response.status_code == 400 and "PRIVATE" not in response.text
