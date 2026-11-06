import asyncio
import io
import json
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from app import audio_service, token_budget
from app.config import settings
from app.database import _connect, create_api_key, init_database
from app.errors import GatewayError
from app.main import app

KEY = "sk-local-audio-test-key-with-thirty-two-characters"
HEADERS = {"Authorization": f"Bearer {KEY}"}


def sound(seconds=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * int(16000 * seconds))
    return output.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "audio.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)
    model = tmp_path / "local-model"
    model.mkdir()
    monkeypatch.setattr(settings, "audio_local_model", str(model))
    monkeypatch.setattr(settings, "audio_backend_url", "")
    with TestClient(app, base_url="http://127.0.0.1") as instance:
        yield instance


async def set_limits(limits):
    async with _connect() as db:
        await db.execute("UPDATE api_keys SET limits_json=?", (json.dumps(limits),))
        await db.commit()


def test_audio_transcription_and_file_attachment_share_key_budget(client, monkeypatch):
    async def local(data, language):
        assert data.startswith(b"RIFF") and language == "en"
        return {"text": "spoken words " * 50, "language": "en"}

    monkeypatch.setattr(audio_service, "local", local)
    client.portal.call(set_limits, {"output_tokens": 4, "token_budget": 200, "budget_period": "daily"})
    response = client.post("/v1/audio/transcriptions", headers=HEADERS, files={"file": ("speech.wav", sound(), "audio/wav")}, data={"model": "transcription", "language": "en"})
    assert response.status_code == 200, response.text
    assert response.json()["truncated"] is True
    assert response.json()["usage"]["input_tokens"] == 50
    assert response.json()["usage"]["output_tokens"] <= 4
    usage = client.get("/v1/usage", headers=HEADERS).json()["usage"]
    assert usage["used_tokens"] == response.json()["usage"]["total_tokens"]
    assert usage["reserved_tokens"] == 0


def test_audio_input_limits_and_missing_backend_are_explicit(client, monkeypatch):
    client.portal.call(set_limits, {"input_tokens": 10})
    response = client.post("/v1/audio/transcriptions", headers=HEADERS, files={"file": ("speech.wav", sound(), "audio/wav")})
    assert response.status_code == 400 and response.json()["error"]["code"] == "input_token_limit_exceeded"
    monkeypatch.setattr(settings, "audio_local_model", "")
    unavailable = client.post("/v1/files", headers=HEADERS, files={"file": ("speech.wav", sound(), "audio/wav")})
    assert unavailable.status_code == 503 and unavailable.json()["error"]["code"] == "audio_provider_not_configured"
    assert client.get("/v1/files", headers=HEADERS).json()["data"] == []


def test_audio_duration_and_unsupported_options_are_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "file_max_audio_seconds", 1)
    too_long = client.post("/v1/audio/transcriptions", headers=HEADERS, files={"file": ("speech.wav", sound(2), "audio/wav")})
    assert too_long.status_code == 400 and too_long.json()["error"]["code"] == "file_extraction_limit"
    unsupported = client.post("/v1/audio/transcriptions", headers=HEADERS, files={"file": ("speech.wav", sound(), "audio/wav")}, data={"prompt": "ignored?"})
    assert unsupported.status_code == 400 and unsupported.json()["error"]["code"] == "unsupported_transcription_feature"


@pytest.mark.asyncio
async def test_remote_audio_uses_configured_endpoint_without_leaking_provider_errors(monkeypatch):
    monkeypatch.setattr(settings, "audio_backend_url", "http://audio-host/v1")
    monkeypatch.setattr(settings, "audio_api_key", "test-audio-secret")
    original = httpx.AsyncClient

    def handler(request):
        assert str(request.url) == "http://audio-host/v1/audio/transcriptions"
        assert request.headers["Authorization"] == "Bearer test-audio-secret"
        assert b"audio.wav" in request.content
        return httpx.Response(401, text="private upstream details")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(GatewayError) as error:
        await audio_service.remote(sound(), "en")
    assert "private" not in str(error.value)


@pytest.mark.asyncio
async def test_cancelled_audio_releases_reservation_and_records_attempt(tmp_path, monkeypatch):
    import base64
    from types import SimpleNamespace

    monkeypatch.setattr(settings, "database_path", tmp_path / "cancel.sqlite3")
    await init_database()
    owner, _ = await create_api_key("audio cancel")
    model = tmp_path / "model"
    model.mkdir()
    monkeypatch.setattr(settings, "audio_local_model", str(model))
    monkeypatch.setattr(settings, "audio_backend_url", "")
    started = asyncio.Event()

    async def local(*_):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(audio_service, "local", local)
    instance = SimpleNamespace(state=SimpleNamespace(audio_processing_slots=asyncio.Semaphore(1)))
    task = asyncio.create_task(audio_service.transcribe(instance, owner.id, base64.b64encode(sound()).decode(), 1))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    usage = await token_budget.status(owner.id)
    assert usage["used_tokens"] == 50 and usage["reserved_tokens"] == 0
