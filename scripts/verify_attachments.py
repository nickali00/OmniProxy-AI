"""Real-model attachment smoke test; all files and keys are temporary."""

import argparse
import asyncio
import io
import json
import logging
import subprocess
import tempfile
from pathlib import Path

import httpx
from PIL import Image

from app.config import settings
from app.database import create_gateway_api
from app.main import app


async def verify(broker_url, model):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="omniproxy-attachments-") as temporary:
        settings.database_url = ""
        settings.admin_auth_required = False
        settings.dashboard_auth_required = False
        settings.require_https = False
        settings.otel_enabled = False
        settings.database_path = Path(temporary) / "gateway.sqlite3"
        settings.bootstrap_api_key = "sk-local-attachments-test-key-with-thirty-two-characters"
        settings.codex_broker_url = broker_url
        image = Image.new("RGB", (96, 96), "red")
        fixtures = []
        for filename, encoding, mime in [("red.png", "PNG", "image/png"), ("red-page.pdf", "PDF", "application/pdf")]:
            content = io.BytesIO()
            image.save(content, format=encoding)
            fixtures.append((filename, content.getvalue(), mime))
        video = Path(temporary) / "red.mp4"
        await asyncio.to_thread(subprocess.run, ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=96x96:d=2", "-c:v", "mpeg4", "-y", str(video)], check=True)
        fixtures.append(("red.mp4", video.read_bytes(), "video/mp4"))
        async with app.router.lifespan_context(app):
            profile, key = await create_gateway_api(name="Attachment E2E", provider="codex", model=model, reasoning_effort="low")
            headers = {"Authorization": f"Bearer {key}"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", timeout=180) as client:
                results = []
                for filename, content, mime in fixtures:
                    uploaded = await client.post("/v1/files", headers=headers, files={"file": (filename, content, mime)})
                    assert uploaded.status_code == 201, uploaded.text
                    file_id = uploaded.json()["id"]
                    response = await client.post("/v1/responses", headers=headers, json={"model": profile.slug, "max_output_tokens": 64, "input": [{"role": "user", "content": [{"type": "input_text", "text": "What is the dominant color of the attached image/page/video frames? Answer only RED or BLUE."}, {"type": "input_file", "file_id": file_id}]}]})
                    assert response.status_code == 200, response.text
                    assert "RED" in response.json()["output_text"].upper()
                    assert (await client.delete(f"/v1/files/{file_id}", headers=headers)).status_code == 200
                    results.append({"format": mime, "upload": True, "model_understood": True, "deleted": True})
                print(json.dumps({"scope": "source_asgi_temporary_database", "model": model, "passed": True, "attachments": results}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker-url", required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    args = parser.parse_args()
    asyncio.run(verify(args.broker_url, args.model))
