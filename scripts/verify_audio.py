"""Real CPU audio → transcription → local chat smoke test with synthetic speech."""

import argparse
import asyncio
import json
import logging
import subprocess
import tempfile
from pathlib import Path

import httpx

from app.config import settings
from app.main import app


async def verify(model_path):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="omniproxy-audio-e2e-") as temporary:
        directory = Path(temporary)
        audio = directory / "speech.wav"
        await asyncio.to_thread(subprocess.run, ["espeak-ng", "-v", "en-us", "-s", "130", "-w", str(audio), "The color is blue. The color is blue."], check=True)
        settings.database_url = ""
        settings.admin_auth_required = False
        settings.dashboard_auth_required = False
        settings.require_https = False
        settings.otel_enabled = False
        settings.database_path = directory / "gateway.sqlite3"
        settings.bootstrap_api_key = "sk-local-audio-test-key-with-thirty-two-characters"
        settings.audio_local_model = model_path
        settings.audio_backend_url = ""
        settings.ollama_base_url = "http://127.0.0.1:11434"
        settings.ollama_model = "qwen3.5:2b"
        headers = {"Authorization": f"Bearer {settings.bootstrap_api_key}"}
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", timeout=180) as client:
            direct = await client.post("/v1/audio/transcriptions", headers=headers, files={"file": ("speech.wav", audio.read_bytes(), "audio/wav")}, data={"model": "transcription", "language": "en"})
            assert direct.status_code == 200, direct.text
            assert "blue" in direct.json()["text"].lower(), direct.json()["text"]
            uploaded = await client.post("/v1/files", headers=headers, files={"file": ("speech.wav", audio.read_bytes(), "audio/wav")})
            assert uploaded.status_code == 201, uploaded.text
            response = await client.post("/v1/chat/completions", headers=headers, json={"model": "local", "messages": [{"role": "user", "content": [{"type": "text", "text": "Which color was mentioned in the audio transcript? Answer with exactly one word."}, {"type": "file", "file": {"file_id": uploaded.json()["id"]}}]}], "max_completion_tokens": 32})
            assert response.status_code == 200, response.text
            assert "blue" in response.json()["choices"][0]["message"]["content"].lower()
            usage = (await client.get("/v1/usage", headers=headers)).json()["usage"]
            assert usage["used_tokens"] > 0 and usage["reserved_tokens"] == 0
            print(json.dumps({"scope": "source_asgi_temporary_database", "audio_backend": "local_cpu", "direct_transcription": True, "audio_attachment": True, "local_model_understood": True, "budget_accounted": True, "passed": True}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", required=True)
    asyncio.run(verify(parser.parse_args().model_path))
