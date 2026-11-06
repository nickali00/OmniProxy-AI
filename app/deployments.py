"""Server-owned upstream targets and failover before any client-visible output."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import weakref
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)

from app import shared_capacity
from app.config import settings
from app.errors import GatewayError


class Deployment(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,40}$")
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=128)
    api_key: SecretStr = SecretStr("")
    capacity_group: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,40}$")
    max_concurrency: int = Field(default=2, ge=1, le=64)
    fallbacks: list[str] = Field(default_factory=list, max_length=4)
    fallback_on: list[Literal["busy", "connect_error", "quota"]] = Field(default_factory=list, max_length=3)
    images: bool = False
    model_config = ConfigDict(extra="forbid")

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        value = value.strip().rstrip("/")
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Invalid deployment URL")
        return value


def configured() -> dict[str, Deployment]:
    try:
        raw = json.loads(settings.upstream_deployments_json)
        if not isinstance(raw, list) or len(raw) > 32:
            raise ValueError()
        deployments = [Deployment.model_validate(item) for item in raw]
        result = {item.id: item for item in deployments}
        if len(result) != len(deployments) or any(target == item.id or target not in result for item in deployments for target in item.fallbacks):
            raise ValueError()
        return result
    except (ValueError, TypeError, ValidationError):
        raise GatewayError(503, "Configurazione deployment upstream non valida.", code="invalid_deployment_configuration") from None


@dataclass
class Capacity:
    active: int = 0
    blocked_until: float = 0


@dataclass
class Statistics:
    attempts: int = 0
    failures: int = 0
    fallbacks: int = 0
    last_error: str | None = None


_states: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_lock = threading.Lock()
_trace: ContextVar[list | None] = ContextVar("upstream_deployment_trace", default=None)


def start_trace():
    result = []
    _trace.set(result)
    return result


def selection():
    trace = _trace.get()
    return trace[-1] if trace else None


def selected(target, fallback):
    trace = _trace.get()
    if trace is not None:
        trace.append({"deployment": target.id, "model": target.model, "fallback": fallback})


def runtime():
    loop = asyncio.get_running_loop()
    with _lock:
        return _states.setdefault(loop, ({}, {}))


def state_for(target):
    capacities, stats = runtime()
    group = target.capacity_group or target.id
    return capacities.setdefault(group, Capacity()), stats.setdefault(target.id, Statistics())


def diagnostics(target):
    capacity, stats = state_for(target)
    return {"id": target.id, "active": capacity.active, "max_concurrency": target.max_concurrency, "cooldown_seconds": max(0, int(capacity.blocked_until - time.monotonic())), "attempts": stats.attempts, "failures": stats.failures, "fallbacks": stats.fallbacks, "last_error": stats.last_error}


async def observe(target):
    return await shared_capacity.diagnostics(target) if settings.database_url else {**diagnostics(target), "scope": "process"}


async def mark_failed(target, exc):
    failed(target, exc)
    if settings.database_url:
        await shared_capacity.failed(target, exc.code, quota_seconds(exc) if exc.code == "upstream_quota_exceeded" else 0)


def targets(alias, *, allow_fallback):
    all_targets = configured()
    primary = all_targets.get(alias.removeprefix("upstream:"))
    if primary is None:
        raise GatewayError(404, "Deployment upstream non disponibile.", code="deployment_not_found")
    return primary, all_targets, [primary] + ([all_targets[name] for name in dict.fromkeys(primary.fallbacks)] if allow_fallback else [])


def retry_reason(exc):
    return {"deployment_busy": "busy", "upstream_unreachable": "connect_error", "upstream_quota_exceeded": "quota"}.get(exc.code)


def quota_seconds(exc):
    try:
        return min(600, max(1, int((exc.headers or {}).get("Retry-After", "60"))))
    except ValueError:
        return 60


def enter(target, all_targets):
    capacity, stats = state_for(target)
    limit = min(item.max_concurrency for item in all_targets.values() if (item.capacity_group or item.id) == (target.capacity_group or target.id))
    if capacity.blocked_until > time.monotonic():
        raise GatewayError(429, "Account upstream temporaneamente in quota.", code="upstream_quota_exceeded", headers={"Retry-After": str(max(1, int(capacity.blocked_until - time.monotonic())))})
    if capacity.active >= limit:
        raise GatewayError(429, "Concorrenza del deployment esaurita.", code="deployment_busy", headers={"Retry-After": "1"})
    capacity.active += 1
    stats.attempts += 1
    return capacity, stats


def failed(target, exc):
    capacity, stats = state_for(target)
    stats.failures += 1
    stats.last_error = exc.code
    if exc.code == "upstream_quota_exceeded":
        capacity.blocked_until = time.monotonic() + quota_seconds(exc)


async def complete(client, request, alias, *, tools=False):
    from app.providers.openai_compatible import OpenAICompatibleProvider

    primary, all_targets, candidates = targets(alias, allow_fallback=not tools and not request.has_client_tools)
    for index, target in enumerate(candidates):
        capacity = lease = None
        try:
            capacity, stats = enter(target, all_targets)
            if settings.database_url:
                lease = await shared_capacity.acquire(target, all_targets, index > 0)
            if index:
                stats.fallbacks += 1
            selected(target, index > 0)
            adapter = OpenAICompatibleProvider(client, deployment=target)
            operation = adapter.complete_with_tools if tools else adapter.complete
            async with asyncio.timeout(settings.provider_completion_timeout_seconds):
                return await operation(request, target.model)
        except TimeoutError:
            timeout = GatewayError(504, "Timeout upstream.", code="upstream_timeout")
            await mark_failed(target, timeout)
            raise timeout from None
        except GatewayError as exc:
            await mark_failed(target, exc)
            if index == len(candidates) - 1 or retry_reason(exc) not in primary.fallback_on:
                raise
        finally:
            if capacity:
                capacity.active -= 1
            if lease:
                await asyncio.shield(shared_capacity.release(lease))


async def stream(client, request, alias):
    from app.providers.openai_compatible import OpenAICompatibleProvider

    primary, all_targets, candidates = targets(alias, allow_fallback=not request.has_client_tools)
    for index, target in enumerate(candidates):
        capacity = source = lease = None
        emitted = False
        try:
            capacity, stats = enter(target, all_targets)
            if settings.database_url:
                lease = await shared_capacity.acquire(target, all_targets, index > 0)
            if index:
                stats.fallbacks += 1
            selected(target, index > 0)
            source = OpenAICompatibleProvider(client, deployment=target).stream(request, target.model)
            async with asyncio.timeout(settings.provider_completion_timeout_seconds):
                async for chunk in source:
                    emitted = True
                    yield chunk
            return
        except TimeoutError:
            timeout = GatewayError(504, "Timeout upstream.", code="upstream_timeout")
            await mark_failed(target, timeout)
            raise timeout from None
        except GatewayError as exc:
            await mark_failed(target, exc)
            if emitted or index == len(candidates) - 1 or retry_reason(exc) not in primary.fallback_on:
                raise
        finally:
            try:
                if source:
                    await source.aclose()
            finally:
                if capacity:
                    capacity.active -= 1
                if lease:
                    await asyncio.shield(shared_capacity.release(lease))
