"""Offline CPU speech generation with the same per-key token policies."""

import asyncio
import json
import math
import os
import shutil
import signal
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import token_budget
from app.config import settings
from app.database import record_usage
from app.errors import GatewayError
from app.request_lifecycle import await_connected
from app.token_counter import count_text_tokens

router = APIRouter()
MIMES = {"wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac", "opus": "audio/ogg", "pcm": "audio/pcm", "aac": "audio/aac"}


class SpeechRequest(BaseModel):
    model: Literal["speech-local"] = "speech-local"
    input: str = Field(min_length=1, max_length=4096)
    voice: Literal["it", "en-us", "en-gb", "es", "fr", "de"] = "it"
    response_format: Literal["wav", "mp3", "flac", "opus", "pcm", "aac"] = "mp3"
    speed: float = Field(default=1, ge=0.5, le=2)
    model_config = ConfigDict(extra="forbid")

    @field_validator("input")
    @classmethod
    def valid_input(cls, value):
        value.encode("utf-8")
        if not value.strip():
            raise ValueError("Empty speech input")
        return value


async def render(payload, seconds):
    with tempfile.TemporaryDirectory(prefix="omniproxy-speech-") as temporary:
        source, output = Path(temporary) / "request.json", Path(temporary) / "audio.data"
        source.write_text(json.dumps({"input": payload.input, "voice": payload.voice, "speed": payload.speed, "format": payload.response_format, "max_seconds": seconds}))
        environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
        worker = await asyncio.create_subprocess_exec(sys.executable, "-m", "app.speech_worker", str(source), str(output), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=environment, start_new_session=True)
        try:
            metadata, _ = await asyncio.wait_for(worker.communicate(), settings.speech_timeout_seconds)
        except (TimeoutError, asyncio.CancelledError) as exc:
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await worker.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise GatewayError(504, "Sintesi vocale oltre il tempo massimo.", code="speech_timeout") from None
        try:
            result = json.loads(metadata)
            if worker.returncode or "error" in result or not output.is_file() or not 0 < output.stat().st_size <= 10 * 1024 * 1024:
                raise ValueError()
            if not isinstance(result.get("truncated"), bool) or not 0 < float(result.get("duration_seconds", 0)) <= seconds + 0.001:
                raise ValueError()
            return output.read_bytes(), result
        except (ValueError, OSError):
            raise GatewayError(502, "Sintesi vocale non riuscita.", code="speech_generation_failed") from None


async def synthesize(request, payload):
    if shutil.which("espeak-ng") is None or shutil.which("ffmpeg") is None:
        raise GatewayError(503, "Runtime di sintesi vocale locale non installato.", code="speech_runtime_missing")
    owner = int(request.state.api_key_id)
    limits = await token_budget.policy(owner)
    input_tokens = count_text_tokens(payload.input)
    if limits.input_tokens and input_tokens > limits.input_tokens:
        raise GatewayError(400, "Testo oltre il limite di ingresso della chiave.", code="input_token_limit_exceeded")
    maximum = max(1, math.ceil(settings.speech_max_seconds * settings.speech_output_tokens_per_second))
    output_limit = limits.output_tokens
    if output_limit is None and (limits.context_tokens or limits.token_budget):
        output_limit = min(settings.default_budget_output_tokens, limits.context_tokens // 4 if limits.context_tokens else maximum)
    output_limit = min(output_limit or maximum, maximum)
    if limits.context_tokens and input_tokens + output_limit > limits.context_tokens:
        raise GatewayError(400, "La richiesta supera il contesto della chiave.", code="context_limit_exceeded")
    request_id = "speech-" + uuid.uuid4().hex
    started, output_tokens, status, error = time.perf_counter(), 0, 200, None
    async with request.app.state.speech_processing_slots:
        output_limit = await token_budget.reserve(request_id, owner, limits, input_tokens, output_limit, lease_seconds=settings.speech_timeout_seconds + 60)
        try:
            content, metadata = await render(payload, min(settings.speech_max_seconds, output_limit / settings.speech_output_tokens_per_second))
            output_tokens = min(output_limit, math.ceil(metadata["duration_seconds"] * settings.speech_output_tokens_per_second))
            headers = {"X-OmniProxy-Tokens-Estimated": "true", "X-OmniProxy-Output-Truncated": str(metadata["truncated"]).lower(), "X-OmniProxy-Input-Tokens": str(input_tokens), "X-OmniProxy-Output-Tokens": str(output_tokens), "X-OmniProxy-Audio-Duration": str(round(metadata["duration_seconds"], 3)), "Cache-Control": "no-store"}
            if payload.response_format == "pcm":
                headers["X-OmniProxy-Audio-Sample-Rate"] = "24000"
                headers["X-OmniProxy-Audio-Channels"] = "1"
            return content, headers
        except asyncio.CancelledError:
            status, error = 499, "client_disconnected"
            raise
        except GatewayError as exc:
            status, error = exc.status_code, exc.code
            raise
        finally:
            await asyncio.shield(token_budget.settle(request_id, input_tokens + output_tokens))
            await asyncio.shield(record_usage(request_id=request_id, api_key_id=owner, requested_model=payload.model, routed_provider="speech_local", resolved_model="espeak-ng", prompt_tokens=input_tokens, completion_tokens=output_tokens, status_code=status, latency_ms=int((time.perf_counter() - started) * 1000), error_code=error))


@router.post("/v1/audio/speech")
async def speech(payload: SpeechRequest, request: Request):
    content, headers = await await_connected(request, synthesize(request, payload))
    usage = await token_budget.status(int(request.state.api_key_id))
    if usage["remaining_tokens"] is not None:
        headers["X-OmniProxy-Budget-Remaining"] = str(usage["remaining_tokens"])
        headers["X-OmniProxy-Budget-Reset"] = usage["resets_at"]
    return Response(content, media_type=MIMES[payload.response_format], headers=headers)
