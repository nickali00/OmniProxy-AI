"""Real neural image generation using a tiny official test checkpoint on CPU.

This validates the adapter and metering, not image quality of the test model.
"""

import argparse
import asyncio
import base64
import io
import json
import logging
import tempfile
from pathlib import Path

import httpx
from PIL import Image

from app.config import settings
from app.database import create_gateway_api
from app.main import app


async def verify(model_path):
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="omniproxy-image-e2e-") as temporary:
        settings.database_url = ""
        settings.admin_auth_required = False
        settings.dashboard_auth_required = False
        settings.require_https = False
        settings.otel_enabled = False
        settings.database_path = Path(temporary) / "gateway.sqlite3"
        settings.bootstrap_api_key = "sk-local-image-test-key-with-thirty-two-characters"
        settings.image_local_model = model_path
        settings.image_backend_url = ""
        settings.image_local_steps = 1
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", timeout=180) as client:
            _, key = await create_gateway_api(name="Image E2E", provider="ollama", model="fixture", reasoning_effort="off", limits={"token_budget": 10000, "budget_period": "daily"})
            auth = {"Authorization": f"Bearer {key}"}
            response = await client.post("/v1/images/generations", headers=auth, json={"model": "image", "prompt": "A blue square on a white background.", "size": "256x256"})
            assert response.status_code == 200, response.text
            raw = base64.b64decode(response.json()["data"][0]["b64_json"])
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                assert image.format == "PNG" and image.size == (256, 256)
            status = (await client.get("/v1/usage", headers=auth)).json()["usage"]
            assert status["reserved_tokens"] == 0 and status["used_tokens"] == response.json()["usage"]["total_tokens"]
            print(json.dumps({"scope": "source_gateway_temporary_database", "backend": "local_cpu_diffusers", "checkpoint": "official_tiny_sdxl_test_fixture", "quality_assessed": False, "valid_neural_png": True, "budget_accounted": True, "passed": True}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", required=True)
    asyncio.run(verify(parser.parse_args().model_path))
