"""Real text → local speech → local transcription and shared budget."""

import argparse
import asyncio
import json
import logging
import tempfile
from pathlib import Path

import httpx

from app.config import settings
from app.database import create_gateway_api
from app.main import app


async def verify(model_path):
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="omniproxy-speech-e2e-") as temporary:
        settings.database_url = ""
        settings.admin_auth_required = False
        settings.dashboard_auth_required = False
        settings.require_https = False
        settings.otel_enabled = False
        settings.database_path = Path(temporary) / "gateway.sqlite3"
        settings.bootstrap_api_key = "sk-local-speech-test-key-with-thirty-two-characters"
        settings.audio_local_model = model_path
        settings.audio_backend_url = ""
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", timeout=180) as client:
            _, key = await create_gateway_api(name="Speech loop", provider="ollama", model="fixture", reasoning_effort="off", limits={"token_budget": 10000, "budget_period": "daily"})
            auth = {"Authorization": f"Bearer {key}"}
            spoken = await client.post("/v1/audio/speech", headers=auth, json={"model": "speech-local", "input": "The color is blue. The color is blue.", "voice": "en-us", "response_format": "wav"})
            assert spoken.status_code == 200, spoken.text
            heard = await client.post("/v1/audio/transcriptions", headers=auth, files={"file": ("speech.wav", spoken.content, "audio/wav")}, data={"model": "transcription", "language": "en"})
            assert heard.status_code == 200, heard.text
            assert "blue" in heard.json()["text"].lower()
            usage = (await client.get("/v1/usage", headers=auth)).json()["usage"]
            assert usage["reserved_tokens"] == 0
            assert usage["used_tokens"] == int(spoken.headers["X-OmniProxy-Input-Tokens"]) + int(spoken.headers["X-OmniProxy-Output-Tokens"]) + heard.json()["usage"]["total_tokens"]
            print(json.dumps({"scope": "source_gateway_temporary_database", "real_speech_generation": True, "real_local_transcription": True, "spoken_content_recognized": True, "shared_budget_accounted": True, "passed": True}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio-model-path", required=True)
    asyncio.run(verify(parser.parse_args().audio_model_path))
