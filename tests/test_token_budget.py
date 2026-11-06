import asyncio
import json
from datetime import UTC, datetime

import pytest

from app import token_budget
from app.config import settings
from app.database import _connect, create_api_key, init_database
from app.errors import GatewayError
from app.schemas import ApiLimits


async def key_with_policy(tmp_path, monkeypatch, limits):
    monkeypatch.setattr(settings, "database_path", tmp_path / "budget.sqlite3")
    await init_database()
    record, _ = await create_api_key("budget key")
    async with _connect() as db:
        await db.execute("UPDATE api_keys SET limits_json=? WHERE id=?", (limits.model_dump_json(), record.id))
        await db.commit()
    return record.id


@pytest.mark.asyncio
async def test_concurrent_reservations_cannot_overspend_and_settlement_is_idempotent(tmp_path, monkeypatch):
    limits = ApiLimits(token_budget=100, budget_period="daily")
    owner = await key_with_policy(tmp_path, monkeypatch, limits)
    results = await asyncio.gather(token_budget.reserve("a", owner, limits, 10, 80), token_budget.reserve("b", owner, limits, 10, 80), return_exceptions=True)
    assert sum(isinstance(item, GatewayError) and item.status_code == 429 for item in results) == 1
    reservation = "a" if results[0] == 80 else "b"
    await token_budget.settle(reservation, 20)
    await token_budget.settle(reservation, 20)
    assert await token_budget.reserve("c", owner, limits, 10, 80) == 70
    status = await token_budget.status(owner)
    assert status["used_tokens"] == 20 and status["reserved_tokens"] == 80
    assert status["remaining_tokens"] == 0
    await token_budget.settle("c", 15)
    assert (await token_budget.status(owner))["remaining_tokens"] == 65


@pytest.mark.asyncio
async def test_expired_reservations_are_charged_conservatively(tmp_path, monkeypatch):
    limits = ApiLimits(token_budget=100, budget_period="weekly")
    owner = await key_with_policy(tmp_path, monkeypatch, limits)
    await token_budget.reserve("lost", owner, limits, 10, 80)
    async with _connect() as db:
        await db.execute("UPDATE key_token_reservations SET expires_at=0")
        await db.commit()
    status = await token_budget.status(owner)
    assert status["used_tokens"] == 90 and status["reserved_tokens"] == 0
    with pytest.raises(GatewayError) as error:
        await token_budget.reserve("next", owner, limits, 10, 10)
    assert error.value.code == "token_budget_exceeded"
    assert "Retry-After" in error.value.headers


@pytest.mark.asyncio
async def test_unlimited_and_historical_usage_when_enabling_budget(tmp_path, monkeypatch):
    limits = ApiLimits()
    owner = await key_with_policy(tmp_path, monkeypatch, limits)
    assert await token_budget.reserve("unlimited", owner, limits, 10, None) is None
    await token_budget.settle("unlimited", 20)
    assert (await token_budget.status(owner))["remaining_tokens"] is None
    async with _connect() as db:
        await db.execute("INSERT INTO usage_logs(request_id,api_key_id,requested_model,routed_provider,resolved_model,total_tokens,status_code) VALUES ('unlimited',?,'test','test','test',20,200)", (owner,))
        await db.execute("UPDATE api_keys SET limits_json=? WHERE id=?", (json.dumps({"token_budget": 100, "budget_period": "monthly"}), owner))
        await db.commit()
    status = await token_budget.status(owner)
    assert status["used_tokens"] == 20 and status["remaining_tokens"] == 80


@pytest.mark.asyncio
async def test_switching_periods_keeps_all_consumption_and_expired_charges(tmp_path, monkeypatch):
    limits = ApiLimits(token_budget=1000, budget_period="daily")
    owner = await key_with_policy(tmp_path, monkeypatch, limits)
    for index, period in enumerate(("daily", "weekly", "monthly", "unlimited", "daily")):
        limits = ApiLimits(token_budget=None if period == "unlimited" else 1000, budget_period=period)
        async with _connect() as db:
            await db.execute("UPDATE api_keys SET limits_json=? WHERE id=?", (limits.model_dump_json(), owner))
            await db.commit()
        assert (await token_budget.status(owner))["used_tokens"] == index * 20
        await token_budget.reserve(f"request-{index}:1", owner, limits, 10, 20)
        await token_budget.settle(f"request-{index}:1", 20)
        # The aggregate HTTP usage log must not double count the reservation.
        async with _connect() as db:
            await db.execute("INSERT INTO usage_logs(request_id,api_key_id,requested_model,routed_provider,resolved_model,total_tokens,status_code) VALUES (?,?,'test','test','test',20,200)", (f"request-{index}", owner))
            await db.commit()
    await token_budget.reserve("crashed:1", owner, limits, 10, 20)
    async with _connect() as db:
        await db.execute("UPDATE key_token_reservations SET expires_at=0 WHERE id='crashed:1'")
        await db.commit()
    assert (await token_budget.status(owner))["used_tokens"] == 130
    async with _connect() as db:
        await db.execute("UPDATE api_keys SET limits_json=? WHERE id=?", (ApiLimits(token_budget=1000, budget_period="monthly").model_dump_json(), owner))
        await db.commit()
    assert (await token_budget.status(owner))["used_tokens"] == 130


@pytest.mark.asyncio
async def test_reservation_history_migrates_from_previous_schema(tmp_path, monkeypatch):
    limits = ApiLimits(token_budget=100, budget_period="daily")
    owner = await key_with_policy(tmp_path, monkeypatch, limits)
    await token_budget.reserve("legacy:1", owner, limits, 10, 20)
    await token_budget.settle("legacy:1", 15)
    async with _connect() as db:
        await db.execute("ALTER TABLE key_token_reservations RENAME TO old_reservations")
        await db.execute("""CREATE TABLE key_token_reservations (
            id TEXT PRIMARY KEY, api_key_id INTEGER NOT NULL REFERENCES api_keys(id),
            period TEXT NOT NULL, window_start REAL NOT NULL, reserved_tokens INTEGER NOT NULL,
            state TEXT NOT NULL, expires_at REAL NOT NULL, actual_tokens INTEGER)""")
        await db.execute("INSERT INTO key_token_reservations SELECT id,api_key_id,period,window_start,reserved_tokens,state,expires_at,actual_tokens FROM old_reservations")
        await db.execute("DROP TABLE old_reservations")
        await db.commit()
    await init_database()
    await init_database()
    assert (await token_budget.status(owner))["used_tokens"] == 15
    async with _connect() as db:
        row = await (await db.execute("SELECT source_request_id,started_at FROM key_token_reservations")).fetchone()
        assert row["source_request_id"] == "legacy" and row["started_at"] > 0


def test_calendar_windows_handle_week_month_and_dst(monkeypatch):
    monkeypatch.setattr(settings, "token_budget_timezone", "Europe/Rome")
    start, end = token_budget.window("daily", datetime(2026, 3, 29, 12, tzinfo=UTC))
    assert end - start == 23 * 3600
    start, end = token_budget.window("weekly", datetime(2026, 11, 5, 12, tzinfo=UTC))
    assert datetime.fromtimestamp(start, UTC).day == 1  # Monday Nov 2 local = Nov 1 23:00 UTC.
    assert end - start == 7 * 86400
    start, end = token_budget.window("monthly", datetime(2026, 12, 31, 12, tzinfo=UTC))
    assert datetime.fromtimestamp(end, UTC).year == 2026
    assert datetime.fromtimestamp(end, UTC).month == 12
    assert end > start
