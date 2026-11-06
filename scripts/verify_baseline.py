"""Run source gateway smoke tests with an isolated database and real providers.

Example: .venv/bin/python -m scripts.verify_baseline --vision-model qwen3-vl:latest
Only model names, timing and pass/fail are emitted; no account secrets are read.
"""

import argparse
import asyncio
import base64
import json
import resource
import statistics
import struct
import tempfile
import time
import zlib
from pathlib import Path

import httpx

from app.config import settings
from app.database import create_gateway_api
from app.main import app


def red_image() -> str:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack("!IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff\x00\x00" * 64) * 64))
    png += chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png).decode()


async def verify(args) -> dict:
    result = {"scope": "source_asgi_isolated_database", "text_model": args.text_model, "tests": {}}
    with tempfile.TemporaryDirectory(prefix="omniproxy-baseline-") as temporary:
        settings.database_url = ""
        settings.admin_auth_required = False
        settings.dashboard_auth_required = False
        settings.require_https = False
        settings.otel_enabled = False
        settings.database_path = Path(temporary) / "baseline.sqlite3"
        settings.provider_vault_key_path = Path(temporary) / "vault.key"
        settings.bootstrap_api_key = "sk-local-baseline-test-key-with-thirty-two-characters"
        settings.ollama_base_url = args.ollama_url
        settings.ollama_model = args.text_model
        if args.codex_broker_url:
            settings.codex_broker_url = args.codex_broker_url
        headers = {"Authorization": f"Bearer {settings.bootstrap_api_key}"}
        async with app.router.lifespan_context(app):  # noqa: SIM117 -- separate application and probe lifetimes
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", timeout=300) as client:
                async def text_request(stream=False):
                    started = time.perf_counter()
                    response = await client.post("/v1/chat/completions", headers=headers, json={"model": "local", "messages": [{"role": "user", "content": "Rispondi soltanto OK."}], "max_completion_tokens": 32, "stream": stream, "temperature": 0})
                    assert response.status_code == 200, response.status_code
                    if stream:
                        assert response.text.endswith("data: [DONE]\n\n")
                    else:
                        assert "OK" in response.json()["choices"][0]["message"]["content"].upper()
                    return round((time.perf_counter() - started) * 1000, 2)
                await text_request()  # Warm up before recording steady state.
                times = [await text_request() for _ in range(3)]
                result["tests"]["text"] = {"passed": True, "latency_ms": times, "median_ms": statistics.median(times)}
                result["tests"]["sse"] = {"passed": True, "latency_ms": await text_request(True)}
                result["tests"]["concurrency_3"] = {"passed": True, "latency_ms": await asyncio.gather(*(text_request() for _ in range(3)))}
                image_message = {"role": "user", "content": [{"type": "text", "text": "What is the dominant color of this image? Answer only RED or BLUE."}, {"type": "image_url", "image_url": {"url": red_image()}}]}
                if args.vision_model:
                    settings.ollama_model = args.vision_model
                    started = time.perf_counter()
                    response = await client.post("/v1/chat/completions", headers=headers, json={"model": "local", "messages": [image_message], "max_completion_tokens": 64, "temperature": 0})
                    assert response.status_code == 200, response.status_code
                    assert "RED" in response.json()["choices"][0]["message"]["content"].upper()
                    result["tests"]["ollama_vision"] = {"passed": True, "model": args.vision_model, "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
                if args.codex_broker_url:
                    profile, key = await create_gateway_api(name="baseline-codex", provider="codex", model=args.codex_model, reasoning_effort="low")
                    started = time.perf_counter()
                    response = await client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={"model": profile.slug, "messages": [image_message], "max_completion_tokens": 64})
                    assert response.status_code == 200, response.status_code
                    assert "RED" in response.json()["choices"][0]["message"]["content"].upper()
                    result["tests"]["codex_vision"] = {"passed": True, "model": args.codex_model, "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
    result["gateway_process_peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--text-model", default="qwen3.5:2b")
    parser.add_argument("--vision-model")
    parser.add_argument("--codex-broker-url")
    parser.add_argument("--codex-model", default="gpt-5.6-sol")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(verify(args))
    report = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(report)
    print(report)


if __name__ == "__main__":
    main()
