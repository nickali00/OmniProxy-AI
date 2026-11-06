"""Image generation through a configured upstream or optional offline model."""

import asyncio
import base64
import importlib.util
import json
import math
import os
import signal
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import token_budget
from app.config import settings
from app.database import record_usage
from app.errors import GatewayError
from app.network_security import enforce_upstream_transport
from app.request_lifecycle import await_connected
from app.token_counter import count_text_tokens

router = APIRouter()
MAX_IMAGE_BYTES = 6 * 1024**2


class ImageRequest(BaseModel):
    model: Literal["image"] = "image"
    prompt: str = Field(min_length=1, max_length=4096)
    n: int = Field(default=1, ge=1, le=4)
    size: str = Field(default="512x512", pattern=r"^(256|512|768|1024|1536|2048)x(256|512|768|1024|1536|2048)$")
    response_format: Literal["b64_json"] = "b64_json"
    model_config = ConfigDict(extra="forbid")

    @field_validator("prompt")
    @classmethod
    def valid_prompt(cls, value):
        if not value.strip():
            raise ValueError("empty prompt")
        value.encode("utf-8")
        return value


def failure(code, status=502):
    return GatewayError(status, "Generazione immagini non disponibile o oltre i limiti configurati.", code=code)


def backend():
    if settings.image_backend_url:
        parts = urlsplit(settings.image_backend_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise failure("image_provider_not_configured", 503)
        return "upstream"
    if settings.image_local_model and Path(settings.image_local_model).is_dir():
        if importlib.util.find_spec("torch") is None or importlib.util.find_spec("diffusers") is None:
            raise failure("image_local_runtime_missing", 503)
        return "local"
    raise failure("image_provider_not_configured", 503)


async def remote(payload):
    await enforce_upstream_transport(settings.image_backend_url)
    headers = {"Authorization": f"Bearer {settings.image_api_key}"} if settings.image_api_key else {}
    try:
        async with (
            httpx.AsyncClient(timeout=settings.image_timeout_seconds) as client,
            client.stream("POST", settings.image_backend_url.rstrip("/") + "/images/generations", headers=headers, json={**payload.model_dump(), "model": settings.image_model}) as response,
        ):
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 32 * 1024**2:
                    raise failure("image_output_limit")
        data = json.loads(body)["data"]
        if not isinstance(data, list) or len(data) != payload.n:
            raise ValueError()
        images = []
        for item in data:
            encoded = item["b64_json"]
            if not isinstance(encoded, str) or len(encoded) > math.ceil(MAX_IMAGE_BYTES / 3) * 4:
                raise ValueError()
            image = base64.b64decode(encoded, validate=True)
            if not 0 < len(image) <= MAX_IMAGE_BYTES:
                raise ValueError()
            images.append(image)
        return images
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise failure("image_upstream_error") from None


async def generate(payload, selected):
    width, height = map(int, payload.size.split("x"))
    with tempfile.TemporaryDirectory(prefix="omniproxy-image-") as temporary:
        directory = Path(temporary)
        if selected == "upstream":
            for index, content in enumerate(await remote(payload)):
                (directory / f"{index}.source").write_bytes(content)
        request_file = directory / "request.json"
        request_file.write_text(json.dumps({"mode": "normalize" if selected == "upstream" else "generate", "prompt": payload.prompt, "width": width, "height": height, "n": payload.n, "steps": settings.image_local_steps, "seed": int.from_bytes(os.urandom(4))}))
        environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONPATH": str(Path(__file__).resolve().parent.parent), "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HOME": str(directory / "cache"), "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "2"}
        worker = await asyncio.create_subprocess_exec(sys.executable, "-m", "app.image_worker", str(request_file), str(directory), settings.image_local_model if selected == "local" else "", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=environment, start_new_session=True)
        try:
            metadata, _ = await asyncio.wait_for(worker.communicate(), settings.image_timeout_seconds)
        except (TimeoutError, asyncio.CancelledError) as exc:
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await worker.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise failure("image_generation_timeout", 504) from None
        try:
            result = json.loads(metadata)
            if "error" in result:
                code = result["error"]
                raise failure(code, 400 if code == "image_prompt_too_long" else 422 if code == "image_generation_filtered" else 503 if code == "image_local_runtime_missing" else 502)
            if worker.returncode or result.get("count") != payload.n:
                raise ValueError()
            images = []
            for index in range(payload.n):
                path = directory / f"{index}.png"
                if not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
                    raise ValueError()
                images.append(path.read_bytes())
            return images
        except (ValueError, OSError):
            raise failure("image_generation_failed") from None


async def metered(request, payload):
    selected = backend()
    width, height = map(int, payload.size.split("x"))
    if width * height > settings.image_max_pixels:
        raise failure("image_size_not_supported", 400)
    owner = int(request.state.api_key_id)
    limits = await token_budget.policy(owner)
    input_tokens = count_text_tokens(payload.prompt)
    expected = max(1, math.ceil(width * height / 1024**2 * settings.image_output_tokens_per_megapixel)) * payload.n
    if limits.input_tokens and input_tokens > limits.input_tokens:
        raise failure("input_token_limit_exceeded", 400)
    if limits.output_tokens and expected > limits.output_tokens:
        raise failure("output_token_limit_exceeded", 400)
    if limits.context_tokens and input_tokens + expected > limits.context_tokens:
        raise failure("context_limit_exceeded", 400)
    request_id = "image-" + uuid.uuid4().hex
    async with request.app.state.image_processing_slots:
        reserved = await token_budget.reserve(request_id, owner, limits, input_tokens, expected, lease_seconds=settings.image_timeout_seconds * 2 + 60)
        if reserved < expected:
            await token_budget.settle(request_id, 0)
            status = await token_budget.status(owner)
            retry = max(1, math.ceil(datetime.fromisoformat(status["resets_at"].replace("Z", "+00:00")).timestamp() - datetime.now(timezone.utc).timestamp()))
            raise GatewayError(429, "Budget insufficiente per un'immagine completa.", code="image_budget_insufficient", headers={"X-OmniProxy-Budget-Reset": status["resets_at"], "Retry-After": str(retry)})
        started, output_tokens, status_code, error_code = time.perf_counter(), expected, 200, None
        try:
            images = await generate(payload, selected)
            return {"created": int(time.time()), "data": [{"b64_json": base64.b64encode(image).decode()} for image in images], "usage": {"input_tokens": input_tokens, "output_tokens": expected, "total_tokens": input_tokens + expected, "estimated": True}}
        except asyncio.CancelledError:
            status_code, error_code = 499, "client_disconnected"
            raise
        except GatewayError as exc:
            status_code, error_code = exc.status_code, exc.code
            if exc.code in {"image_prompt_too_long", "image_local_runtime_missing"}:
                output_tokens = 0
            raise
        finally:
            await asyncio.shield(token_budget.settle(request_id, input_tokens + output_tokens))
            await asyncio.shield(record_usage(request_id=request_id, api_key_id=owner, requested_model=payload.model, routed_provider="image_" + selected, resolved_model="local-diffusers" if selected == "local" else settings.image_model, prompt_tokens=input_tokens, completion_tokens=output_tokens, status_code=status_code, latency_ms=int((time.perf_counter() - started) * 1000), error_code=error_code))


@router.post("/v1/images/generations")
async def create_image(payload: ImageRequest, request: Request):
    result = await await_connected(request, metered(request, payload))
    usage = await token_budget.status(int(request.state.api_key_id))
    headers = {"Cache-Control": "no-store", "X-OmniProxy-Tokens-Estimated": "true"}
    if usage["remaining_tokens"] is not None:
        headers["X-OmniProxy-Budget-Remaining"] = str(usage["remaining_tokens"])
        headers["X-OmniProxy-Budget-Reset"] = usage["resets_at"]
    return JSONResponse(result, headers=headers)
