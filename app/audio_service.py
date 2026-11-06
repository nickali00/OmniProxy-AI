"""Local or configured OpenAI-compatible transcription with per-key accounting."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import signal
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiosqlite
import httpx

from app import token_budget
from app.config import settings
from app.database import record_usage
from app.errors import GatewayError
from app.network_security import enforce_upstream_transport
from app.token_counter import clip_text_tokens, count_text_tokens

logger = logging.getLogger(__name__)


def error(code, status=502):
    return GatewayError(status, {
        "audio_provider_not_configured": "Configura un trascrittore audio locale o compatibile OpenAI.",
        "audio_local_runtime_missing": "Il runtime audio locale non è installato.",
        "audio_transcription_timeout": "Trascrizione oltre il tempo massimo.",
        "audio_no_speech": "Nessun parlato riconosciuto nell'audio.",
    }.get(code, "Trascrizione audio non riuscita."), code=code)


def backend():
    if settings.audio_backend_url:
        parts = urlsplit(settings.audio_backend_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise error("audio_provider_not_configured", 503)
        return "upstream"
    if settings.audio_local_model and Path(settings.audio_local_model).is_dir():
        return "local"
    raise error("audio_provider_not_configured", 503)


async def remote(audio: bytes, language: str | None):
    await enforce_upstream_transport(settings.audio_backend_url)
    headers = {"Authorization": f"Bearer {settings.audio_api_key}"} if settings.audio_api_key else {}
    data = {"model": settings.audio_model}
    if language:
        data["language"] = language
    try:
        async with (
            httpx.AsyncClient(timeout=settings.audio_timeout_seconds) as client,
            client.stream("POST", settings.audio_backend_url.rstrip("/") + "/audio/transcriptions", headers=headers, files={"file": ("audio.wav", audio, "audio/wav")}, data=data) as response,
        ):
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise error("audio_invalid_response")
        result = json.loads(body)
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise error("audio_invalid_response")
        return result
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise error("audio_transcription_failed") from exc


async def local(audio: bytes, language: str | None):
    with tempfile.TemporaryDirectory(prefix="omniproxy-audio-") as temporary:
        source = Path(temporary) / "audio.wav"
        source.write_bytes(audio)
        environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONPATH": str(Path(__file__).resolve().parent.parent), "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "2", "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1"}
        arguments = [sys.executable, "-m", "app.audio_worker", str(source), settings.audio_local_model]
        if language:
            arguments.extend(["--language", language])
        worker = await asyncio.create_subprocess_exec(*arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=environment, start_new_session=True)
        try:
            output, _ = await asyncio.wait_for(worker.communicate(), settings.audio_timeout_seconds)
        except (TimeoutError, asyncio.CancelledError) as exc:
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await worker.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise error("audio_transcription_timeout", 422) from exc
        if worker.returncode or len(output) > 2 * 1024 * 1024:
            raise error("audio_transcription_failed")
        try:
            result = json.loads(output)
        except ValueError as exc:
            raise error("audio_transcription_failed") from exc
        if "error" in result:
            raise error(result["error"], 503 if result["error"] == "audio_local_runtime_missing" else 502)
        return result


async def transcribe(app, owner: int, encoded_audio: str, duration: float, language: str | None = None) -> dict:
    selected = backend()
    limits = await token_budget.policy(owner)
    estimated_input = max(1, math.ceil(duration * settings.audio_input_tokens_per_second))
    if limits.input_tokens is not None and estimated_input > limits.input_tokens:
        raise GatewayError(400, "L'audio supera il limite stimato di ingresso della chiave.", code="input_token_limit_exceeded")
    output_limit = limits.output_tokens
    if output_limit is None and (limits.token_budget is not None or limits.context_tokens is not None):
        output_limit = settings.default_budget_output_tokens
        if limits.context_tokens:
            output_limit = min(output_limit, max(1, limits.context_tokens // 4))
    if limits.context_tokens and estimated_input + (output_limit or 0) > limits.context_tokens:
        raise GatewayError(400, "L'audio supera il contesto configurato per la chiave.", code="context_limit_exceeded")
    request_id = f"audio-{uuid.uuid4().hex}"
    started = time.perf_counter()
    output_tokens, status_code, error_code = 0, 200, None
    async with app.state.audio_processing_slots:
        output_limit = await token_budget.reserve(request_id, owner, limits, estimated_input, output_limit, lease_seconds=settings.audio_timeout_seconds + 60)
        try:
            audio = base64.b64decode(encoded_audio, validate=True)
            result = await (local(audio, language) if selected == "local" else remote(audio, language))
            if not isinstance(result, dict) or not isinstance(result.get("text"), str):
                raise error("audio_invalid_response")
            text = result["text"].strip()
            if len(text) > 250_000:
                raise error("audio_invalid_response")
            if not text:
                raise error("audio_no_speech", 422)
            truncated = output_limit is not None and count_text_tokens(text) > output_limit
            if truncated:
                text = clip_text_tokens(text, output_limit)
            output_tokens = count_text_tokens(text)
            return {"text": text, "language": result.get("language", language), "truncated": truncated, "usage": {"input_tokens": estimated_input, "output_tokens": output_tokens, "total_tokens": estimated_input + output_tokens, "estimated": True}, "model": "local-transcription" if selected == "local" else settings.audio_model}
        except asyncio.CancelledError:
            status_code, error_code = 499, "client_disconnected"
            raise
        except GatewayError as exc:
            status_code, error_code = exc.status_code, exc.code
            raise
        finally:
            await asyncio.shield(token_budget.settle(request_id, estimated_input + output_tokens))
            try:
                await asyncio.shield(record_usage(request_id=request_id, api_key_id=owner, requested_model="transcription", routed_provider=f"audio_{selected}", resolved_model="local-transcription" if selected == "local" else settings.audio_model, prompt_tokens=estimated_input, completion_tokens=output_tokens, status_code=status_code, latency_ms=int((time.perf_counter() - started) * 1000), error_code=error_code))
            except aiosqlite.Error:
                logger.error("Audio usage storage unavailable")


async def enrich(app, owner: int, result: dict, language: str | None = None) -> dict:
    encoded = result.get("normalized_audio")
    if encoded is None:
        return result
    transcription = await transcribe(app, owner, encoded, result["metadata"]["duration_seconds"], language)
    result = {key: value for key, value in result.items() if key != "normalized_audio"}
    result["parts"] = [{"type": "text", "text": "[audio transcript]\n" + transcription["text"]}]
    result["metadata"]["transcription"] = {key: value for key, value in transcription.items() if key != "text"}
    if transcription["truncated"]:
        result["warnings"].append("Transcript truncated by the key output limit.")
    return result
