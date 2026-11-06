"""Database-backed upstream capacity for PostgreSQL replicas."""

import logging
import time
import uuid

from app.config import settings
from app.database import _connect
from app.errors import GatewayError

logger = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS upstream_capacity_groups (
 id TEXT PRIMARY KEY, max_concurrency INTEGER NOT NULL, blocked_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS upstream_capacity_leases (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES upstream_capacity_groups(id),
 deployment_id TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_upstream_capacity_group ON upstream_capacity_leases(group_id,expires_at);
CREATE TABLE IF NOT EXISTS upstream_statistics (
 id TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
 fallbacks INTEGER NOT NULL DEFAULT 0, last_error TEXT
);
"""


async def init_capacity():
    async with _connect() as db:
        await db.executescript(SCHEMA)


async def acquire(target, all_targets, fallback=False):
    group = target.capacity_group or target.id
    limit = min(item.max_concurrency for item in all_targets.values() if (item.capacity_group or item.id) == group)
    lease = "cap_" + uuid.uuid4().hex
    now = time.time()
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("DELETE FROM upstream_capacity_leases WHERE expires_at<=?", (now,))
        await db.execute("INSERT INTO upstream_capacity_groups(id,max_concurrency) VALUES (?,?) ON CONFLICT(id) DO NOTHING", (group, limit))
        row = await (await db.execute("SELECT * FROM upstream_capacity_groups WHERE id=?", (group,))).fetchone()
        active = (await (await db.execute("SELECT count(*) FROM upstream_capacity_leases WHERE group_id=?", (group,))).fetchone())[0]
        if row["max_concurrency"] != limit:
            if active:
                raise GatewayError(503, "Configurazione capacità non uniforme tra repliche.", code="capacity_configuration_changed")
            await db.execute("UPDATE upstream_capacity_groups SET max_concurrency=? WHERE id=?", (limit, group))
        if row["blocked_until"] > now:
            raise GatewayError(429, "Account upstream temporaneamente in quota.", code="upstream_quota_exceeded", headers={"Retry-After": str(max(1, int(row["blocked_until"] - now)))})
        if active >= limit:
            raise GatewayError(429, "Concorrenza condivisa del deployment esaurita.", code="deployment_busy", headers={"Retry-After": "1"})
        await db.execute("INSERT INTO upstream_capacity_leases VALUES (?,?,?,?)", (lease, group, target.id, now + settings.provider_completion_timeout_seconds + 60))
        await db.execute("INSERT INTO upstream_statistics(id,attempts,fallbacks) VALUES (?,1,?) ON CONFLICT(id) DO UPDATE SET attempts=upstream_statistics.attempts+1,fallbacks=upstream_statistics.fallbacks+excluded.fallbacks", (target.id, int(fallback)))
        await db.commit()
    return lease


async def release(lease):
    try:
        async with _connect() as db:
            await db.execute("DELETE FROM upstream_capacity_leases WHERE id=?", (lease,))
            await db.commit()
    except Exception:  # noqa: BLE001 -- preserve response; stale lease expires conservatively
        logger.warning("Unable to release upstream capacity; lease will expire")


async def failed(target, code, seconds=0):
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("INSERT INTO upstream_statistics(id,failures,last_error) VALUES (?,1,?) ON CONFLICT(id) DO UPDATE SET failures=upstream_statistics.failures+1,last_error=excluded.last_error", (target.id, code))
        if seconds:
            await db.execute("UPDATE upstream_capacity_groups SET blocked_until=CASE WHEN blocked_until>? THEN blocked_until ELSE ? END WHERE id=?", (time.time() + seconds, time.time() + seconds, target.capacity_group or target.id))
        await db.commit()


async def diagnostics(target):
    async with _connect() as db:
        group = await (await db.execute("SELECT * FROM upstream_capacity_groups WHERE id=?", (target.capacity_group or target.id,))).fetchone()
        stats = await (await db.execute("SELECT * FROM upstream_statistics WHERE id=?", (target.id,))).fetchone()
        active = (await (await db.execute("SELECT count(*) FROM upstream_capacity_leases WHERE group_id=? AND expires_at>?", (target.capacity_group or target.id, time.time()))).fetchone())[0]
    return {"id": target.id, "active": active, "max_concurrency": group["max_concurrency"] if group else target.max_concurrency, "cooldown_seconds": max(0, int(group["blocked_until"] - time.time())) if group else 0, "attempts": stats["attempts"] if stats else 0, "failures": stats["failures"] if stats else 0, "fallbacks": stats["fallbacks"] if stats else 0, "last_error": stats["last_error"] if stats else None, "scope": "database"}
