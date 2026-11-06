"""Atomic reservations for per-key estimated-token budgets."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import settings
from app.database import _connect
from app.errors import GatewayError
from app.schemas import ApiLimits


def window(period: str, now: datetime | None = None) -> tuple[float, float]:
    zone = ZoneInfo(settings.token_budget_timezone)
    current = (now or datetime.now(UTC)).astimezone(zone)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "daily":
        end = start + timedelta(days=1)
    elif period == "weekly":
        start -= timedelta(days=start.weekday())
        end = start + timedelta(days=7)
    elif period == "monthly":
        start = start.replace(day=1)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    else:
        return 0.0, 253402300799.0
    return start.timestamp(), end.timestamp()


async def policy(owner: int) -> ApiLimits:
    async with _connect() as db:
        cursor = await db.execute("SELECT limits_json FROM api_keys WHERE id=?", (owner,))
        row = await cursor.fetchone()
    if row is None:
        raise GatewayError(401, "Chiave API non disponibile.", code="invalid_api_key")
    return ApiLimits.model_validate(json.loads(row["limits_json"]))


async def _expire(db):
    cursor = await db.execute("SELECT * FROM key_token_reservations WHERE state='pending' AND expires_at<?", (time.time(),))
    for row in await cursor.fetchall():
        await db.execute("UPDATE key_token_windows SET used_tokens=used_tokens+?,reserved_tokens=reserved_tokens-? WHERE api_key_id=? AND period=? AND window_start=?", (row["reserved_tokens"], row["reserved_tokens"], row["api_key_id"], row["period"], row["window_start"]))
        await db.execute("UPDATE key_token_reservations SET state='settled',actual_tokens=reserved_tokens WHERE id=?", (row["id"],))


async def expire_reservations():
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await _expire(db)
        await db.commit()


async def _ensure_window(db, owner, period, start, end):
    # Reconstruct consumption across policy changes from the durable reservation
    # ledger. Aggregate usage logs are included only for unmetered legacy calls;
    # matching request IDs must not charge summaries/MCP/audio twice.
    cursor = await db.execute("""
        SELECT COALESCE(SUM(total_tokens),0) FROM usage_logs AS u
        WHERE api_key_id=? AND CAST(strftime('%s',created_at) AS REAL)>=?
          AND CAST(strftime('%s',created_at) AS REAL)<?
          AND NOT EXISTS (SELECT 1 FROM key_token_reservations AS r
                          WHERE r.api_key_id=u.api_key_id AND r.source_request_id=u.request_id)
        """, (owner, start, end))
    previous = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COALESCE(SUM(actual_tokens),0) FROM key_token_reservations WHERE api_key_id=? AND state='settled' AND started_at>=? AND started_at<?", (owner, start, end))
    used = previous + (await cursor.fetchone())[0]
    await db.execute("""INSERT INTO key_token_windows(api_key_id,period,window_start,window_end,used_tokens)
        VALUES (?,?,?,?,?) ON CONFLICT(api_key_id,period,window_start)
        DO UPDATE SET used_tokens=excluded.used_tokens""", (owner, period, start, end, used))


async def reserve(reservation_id: str, owner: int, limits: ApiLimits, prompt_tokens: int, output_tokens: int | None, *, lease_seconds: float | None = None) -> int | None:
    start, end = window(limits.budget_period)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await _expire(db)
        cursor = await db.execute("SELECT limits_json,is_active,status FROM api_keys WHERE id=?", (owner,))
        key = await cursor.fetchone()
        if key is None or not key["is_active"] or key["status"] != "active":
            raise GatewayError(401, "Chiave API non disponibile.", code="invalid_api_key")
        if ApiLimits.model_validate(json.loads(key["limits_json"])) != limits:
            raise GatewayError(409, "I limiti della chiave sono cambiati. Ripeti la richiesta.", code="key_limits_changed")
        await _ensure_window(db, owner, limits.budget_period, start, end)
        cursor = await db.execute("SELECT used_tokens,reserved_tokens FROM key_token_windows WHERE api_key_id=? AND period=? AND window_start=?", (owner, limits.budget_period, start))
        budget = await cursor.fetchone()
        reserved = 0
        if limits.token_budget is not None:
            available = limits.token_budget - budget["used_tokens"] - budget["reserved_tokens"] - prompt_tokens
            if available < 1:
                await db.commit()  # Persist conservative recovery of expired reservations.
                raise GatewayError(429, "Budget token della chiave esaurito.", error_type="rate_limit_error", code="token_budget_exceeded", headers={"Retry-After": str(max(1, int(end - time.time()))), "X-OmniProxy-Budget-Reset": datetime.fromtimestamp(end, UTC).isoformat()})
            output_tokens = min(output_tokens or settings.default_budget_output_tokens, available)
            reserved = prompt_tokens + output_tokens
        await db.execute("UPDATE key_token_windows SET reserved_tokens=reserved_tokens+? WHERE api_key_id=? AND period=? AND window_start=?", (reserved, owner, limits.budget_period, start))
        now = time.time()
        await db.execute("""INSERT INTO key_token_reservations
            (id,api_key_id,period,window_start,reserved_tokens,state,expires_at,actual_tokens,started_at,source_request_id)
            VALUES (?,?,?,?,?,'pending',?,NULL,?,?)""", (reservation_id, owner, limits.budget_period, start, reserved, now + max(120, settings.provider_completion_timeout_seconds + 60, settings.ollama_request_timeout_seconds + 60, lease_seconds or 0), now, reservation_id.rsplit(":", 1)[0] if ":" in reservation_id else reservation_id))
        await db.commit()
    return output_tokens


async def settle(reservation_id: str, actual_tokens: int):
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute("SELECT * FROM key_token_reservations WHERE id=? AND state='pending'", (reservation_id,))
        row = await cursor.fetchone()
        if row is None:
            return
        charged = max(0, actual_tokens)
        if row["period"] != "unlimited":
            charged = min(charged, row["reserved_tokens"])
        await db.execute("UPDATE key_token_windows SET used_tokens=used_tokens+?,reserved_tokens=reserved_tokens-? WHERE api_key_id=? AND period=? AND window_start=?", (charged, row["reserved_tokens"], row["api_key_id"], row["period"], row["window_start"]))
        await db.execute("UPDATE key_token_reservations SET state='settled',actual_tokens=? WHERE id=?", (charged, reservation_id))
        await db.commit()


async def status(owner: int) -> dict:
    limits = await policy(owner)
    start, end = window(limits.budget_period)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await _expire(db)
        await _ensure_window(db, owner, limits.budget_period, start, end)
        cursor = await db.execute("SELECT used_tokens,reserved_tokens FROM key_token_windows WHERE api_key_id=? AND period=? AND window_start=?", (owner, limits.budget_period, start))
        row = await cursor.fetchone()
        await db.commit()
    used, reserved = row["used_tokens"], row["reserved_tokens"]
    return {"period": limits.budget_period, "token_budget": limits.token_budget, "used_tokens": used, "reserved_tokens": reserved, "remaining_tokens": max(0, limits.token_budget - used - reserved) if limits.token_budget is not None else None, "resets_at": datetime.fromtimestamp(end, UTC).isoformat() if limits.token_budget is not None else None, "timezone": settings.token_budget_timezone, "estimated": True}
