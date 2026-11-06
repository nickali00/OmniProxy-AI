"""SQLite transactions for jobs, grants and their durable event log."""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from app.config import settings
from app.database import _connect, record_schema_version
from app.errors import GatewayError

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_workspaces (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, config_json TEXT NOT NULL,
    writable INTEGER NOT NULL CHECK(writable IN (0,1))
);
CREATE TABLE IF NOT EXISTS agent_workspace_grants (
    workspace_id TEXT NOT NULL REFERENCES agent_workspaces(id) ON DELETE CASCADE,
    api_key_id INTEGER NOT NULL REFERENCES api_keys(id),
    PRIMARY KEY(workspace_id, api_key_id)
);
CREATE TABLE IF NOT EXISTS agent_jobs (
    id TEXT PRIMARY KEY, api_key_id INTEGER NOT NULL REFERENCES api_keys(id),
    profile_id INTEGER NOT NULL, workspace_id TEXT NOT NULL REFERENCES agent_workspaces(id),
    idempotency_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','awaiting_approval','applying','completed','cancelled','failed')),
    lease_until REAL, data_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    worker_token TEXT,
    UNIQUE(api_key_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_state ON agent_jobs(state,created_at);
CREATE TABLE IF NOT EXISTS agent_job_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES agent_jobs(id),
    type TEXT NOT NULL, data_json TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_job_events_job ON agent_job_events(job_id,seq);
CREATE TABLE IF NOT EXISTS agent_job_resumptions (
 job_id TEXT NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
 idempotency_hash TEXT NOT NULL, fingerprint TEXT NOT NULL,
 revision INTEGER NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(job_id,idempotency_hash)
);
CREATE TABLE IF NOT EXISTS agent_job_profiles (
 id TEXT PRIMARY KEY, api_key_id INTEGER NOT NULL REFERENCES api_keys(id),
 workspace_id TEXT NOT NULL REFERENCES agent_workspaces(id), name TEXT NOT NULL,
 config_json TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_job_tombstones (
 api_key_id INTEGER NOT NULL REFERENCES api_keys(id), idempotency_hash TEXT NOT NULL,
 deleted_job_id TEXT NOT NULL, deleted_at REAL NOT NULL,
 PRIMARY KEY(api_key_id,idempotency_hash)
);
"""
TERMINAL = {"completed", "cancelled", "failed"}
ACTIVE = {"queued", "running", "awaiting_approval", "applying"}
UNSET = object()


def error(code: str, status=409):
    return GatewayError(status, {
        "job_not_found": "Lavoro non trovato.", "workspace_not_found": "Workspace non trovato.",
        "job_conflict": "La richiesta è in conflitto con un lavoro esistente.",
        "job_limit_exceeded": "Limite di lavori attivi raggiunto.",
        "invalid_job_state": "Lo stato del lavoro non consente questa operazione.",
        "approval_required": "È necessaria l'approvazione della proposta corrente.",
    }.get(code, "Operazione lavoro non disponibile."), code=code)


async def init_jobs():
    async with _connect() as db:
        await db.executescript(SCHEMA)
        if settings.database_url:
            await db.execute("ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS worker_token TEXT")
        else:
            columns = await (await db.execute("PRAGMA table_info(agent_jobs)")).fetchall()
            if "worker_token" not in {row["name"] for row in columns}:
                await db.execute("ALTER TABLE agent_jobs ADD COLUMN worker_token TEXT")
        await db.commit()
        await record_schema_version(db, "jobs", 3)


async def save_workspace(name: str, config: dict, writable: bool, grants: list[int]) -> dict:
    workspace_id = f"ws_{uuid.uuid4().hex}"
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        for key in grants:
            cursor = await db.execute("SELECT id FROM api_keys WHERE id=? AND is_active=1", (key,))
            if await cursor.fetchone() is None:
                raise error("workspace_invalid_grant", 400)
        await db.execute("INSERT INTO agent_workspaces VALUES (?,?,?,?)", (workspace_id, name, json.dumps(config), int(writable)))
        await db.executemany("INSERT INTO agent_workspace_grants VALUES (?,?)", [(workspace_id, key) for key in set(grants)])
        await db.commit()
    return {"id": workspace_id, "name": name, "writable": writable, **config}


async def get_workspace(workspace_id: str, owner: int | None = None) -> dict:
    async with _connect() as db:
        query = "SELECT w.* FROM agent_workspaces w WHERE w.id=?"
        params = [workspace_id]
        if owner is not None:
            query += " AND EXISTS(SELECT 1 FROM agent_workspace_grants g WHERE g.workspace_id=w.id AND g.api_key_id=?)"
            params.append(owner)
        cursor = await db.execute(query, params)
        row = await cursor.fetchone()
    if row is None:
        raise error("workspace_not_found", 404)
    return {"id": row["id"], "name": row["name"], "writable": bool(row["writable"]), **json.loads(row["config_json"])}


async def list_workspaces(owner: int) -> list[dict]:
    async with _connect() as db:
        cursor = await db.execute("SELECT w.* FROM agent_workspaces w JOIN agent_workspace_grants g ON g.workspace_id=w.id WHERE g.api_key_id=? ORDER BY w.name", (owner,))
        rows = await cursor.fetchall()
    return [{"id": row["id"], "name": row["name"], "writable": bool(row["writable"]), "paths": json.loads(row["config_json"])["paths"], "test_profiles": json.loads(row["config_json"]).get("test_profiles", [])} for row in rows]


def _job(row) -> dict:
    return {key: row[key] for key in ("id", "api_key_id", "profile_id", "workspace_id", "state", "lease_until", "created_at", "updated_at", "worker_token")} | {"data": json.loads(row["data_json"])}


async def get_job(job_id: str, owner: int | None = None) -> dict:
    async with _connect() as db:
        cursor = await db.execute("SELECT * FROM agent_jobs WHERE id=?" + (" AND api_key_id=?" if owner is not None else ""), (job_id, owner) if owner is not None else (job_id,))
        row = await cursor.fetchone()
    if row is None:
        raise error("job_not_found", 404)
    return _job(row)


async def _event(db, job_id, kind, data):
    await db.execute("INSERT INTO agent_job_events(job_id,type,data_json,created_at) VALUES (?,?,?,?)", (job_id, kind, json.dumps(data), time.time()))


async def _cancel_tests(db, job_id):
    await db.execute("UPDATE agent_job_tests SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,updated_at=? WHERE job_id=? AND state IN ('queued','running')", (time.time(), job_id))


async def create_job(owner: int, profile, workspace: dict, instruction: str, paths: list[str], key: str, *, job_profile=None) -> tuple[dict, bool]:
    payload = {"type": "repo_patch", "instruction": instruction, "paths": paths, "contract_version": "1", "revision": 0, "route": {"provider": profile.provider, "model": profile.model, "reasoning_effort": profile.reasoning_effort}}
    if job_profile:
        payload["job_profile"] = job_profile
    fingerprint = hashlib.sha256(json.dumps([workspace["id"], profile.id, payload], sort_keys=True).encode()).hexdigest()
    job_id = f"job_{uuid.uuid4().hex}"
    now = time.time()
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        tombstone = await (await db.execute("SELECT 1 FROM agent_job_tombstones WHERE api_key_id=? AND idempotency_hash=?", (owner, hashlib.sha256(key.encode()).hexdigest()))).fetchone()
        if tombstone:
            raise error("job_history_expired", 410)
        cursor = await db.execute("SELECT * FROM agent_jobs WHERE api_key_id=? AND idempotency_key=?", (owner, key))
        previous = await cursor.fetchone()
        if previous is not None:
            if previous["fingerprint"] != fingerprint:
                raise error("job_conflict")
            return _job(previous), False
        cursor = await db.execute("SELECT count(*) FROM agent_jobs WHERE api_key_id=? AND state IN ('queued','running','awaiting_approval','applying')", (owner,))
        if (await cursor.fetchone())[0] >= settings.jobs_max_active_per_key:
            raise error("job_limit_exceeded", 429)
        cursor = await db.execute("SELECT 1 FROM agent_jobs WHERE workspace_id=? AND state IN ('queued','running','awaiting_approval','applying')", (workspace["id"],))
        if await cursor.fetchone() is not None:
            raise error("job_conflict")
        await db.execute("INSERT INTO agent_jobs(id,api_key_id,profile_id,workspace_id,idempotency_key,fingerprint,state,lease_until,data_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (job_id, owner, profile.id, workspace["id"], key, fingerprint, "queued", None, json.dumps(payload), now, now))
        await _event(db, job_id, "job.state", {"state": "queued"})
        await db.commit()
    return await get_job(job_id), True


async def transition(job_id: str, expected: set[str], state: str, updates: dict | None = None, *, event_data: dict | None = None, worker_token=UNSET, expired_only=False) -> dict | None:
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute("SELECT * FROM agent_jobs WHERE id=?", (job_id,))
        row = await cursor.fetchone()
        if row is None or row["state"] not in expected:
            return None
        if worker_token is not UNSET and row["worker_token"] != worker_token:
            return None
        if worker_token is not UNSET and not expired_only and row["state"] == "running" and (row["lease_until"] or 0) <= time.time():
            return None
        if expired_only and (row["lease_until"] is None or row["lease_until"] >= time.time()):
            return None
        data = json.loads(row["data_json"])
        data.update(updates or {})
        if state == "cancelled" or row["state"] == "awaiting_approval" and state == "queued":
            await _cancel_tests(db, job_id)
        lease = time.time() + settings.jobs_lease_seconds if state in {"running", "applying"} else None
        token = row["worker_token"] if state == "running" else None
        await db.execute("UPDATE agent_jobs SET state=?, data_json=?, lease_until=?, updated_at=?, worker_token=? WHERE id=?", (state, json.dumps(data), lease, time.time(), token, job_id))
        await _event(db, job_id, "job.state", {"state": state, **(event_data or {})})
        await db.commit()
    return await get_job(job_id)


async def claim() -> dict | None:
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute("SELECT id FROM agent_jobs WHERE state='queued' ORDER BY created_at LIMIT 1")
        row = await cursor.fetchone()
        if row is None:
            return None
        now = time.time()
        await db.execute("UPDATE agent_jobs SET state='running',lease_until=?,updated_at=?,worker_token=? WHERE id=?", (now + settings.jobs_lease_seconds, now, uuid.uuid4().hex, row["id"]))
        await _event(db, row["id"], "job.state", {"state": "running"})
        await db.commit()
    return await get_job(row["id"])


async def heartbeat(job_id: str, worker_token=None):
    async with _connect() as db:
        # The generation worker owns only 'running'. A delayed heartbeat must
        # never extend a subsequent application/recovery phase's lease.
        now = time.time()
        cursor = await db.execute("UPDATE agent_jobs SET lease_until=? WHERE id=? AND state='running' AND lease_until>? AND (worker_token=? OR (worker_token IS NULL AND ? IS NULL))", (now + settings.jobs_lease_seconds, job_id, now, worker_token, worker_token))
        await db.commit()
        return cursor.rowcount == 1


async def expired_jobs() -> list[dict]:
    async with _connect() as db:
        cursor = await db.execute("SELECT * FROM agent_jobs WHERE state IN ('running','applying') AND lease_until < ?", (time.time(),))
        return [_job(row) for row in await cursor.fetchall()]


async def events(job_id: str, cursor: int) -> list[dict]:
    async with _connect() as db:
        rows = await db.execute("SELECT * FROM agent_job_events WHERE job_id=? AND seq>? ORDER BY seq LIMIT 100", (job_id, cursor))
        return [{"id": row["seq"], "type": row["type"], "data": json.loads(row["data_json"]), "created_at": row["created_at"]} for row in await rows.fetchall()]


async def list_jobs(owner, limit=50, after=None, state=None):
    parameters, query = [owner], "SELECT id,profile_id,workspace_id,state,created_at,updated_at FROM agent_jobs WHERE api_key_id=?"
    if after:
        previous = await get_job(after, owner)
        query += " AND (created_at<? OR (created_at=? AND id<?))"
        parameters.extend([previous["created_at"], previous["created_at"], after])
    if state:
        query += " AND state=?"
        parameters.append(state)
    async with _connect() as db:
        rows = await (await db.execute(query + " ORDER BY created_at DESC,id DESC LIMIT ?", [*parameters, limit + 1])).fetchall()
    return {"object": "list", "data": [dict(row) for row in rows[:limit]], "has_more": len(rows) > limit, "last_id": rows[min(limit, len(rows)) - 1]["id"] if rows else None}


async def resume_job(job_id, owner, profile, instruction, key):
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps({"instruction": instruction}, sort_keys=True).encode()).hexdigest()
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute("SELECT * FROM agent_jobs WHERE id=? AND api_key_id=?", (job_id, owner))).fetchone()
        if row is None:
            raise error("job_not_found", 404)
        previous = await (await db.execute("SELECT fingerprint FROM agent_job_resumptions WHERE job_id=? AND idempotency_hash=?", (job_id, key_hash))).fetchone()
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise error("job_conflict")
            return _job(row), False
        data = json.loads(row["data_json"])
        if row["state"] not in {"failed", "cancelled"}:
            raise error("invalid_job_state")
        if data.get("error_code") == "workspace_recovery_required":
            raise error("workspace_recovery_required")
        granted = await (await db.execute("SELECT 1 FROM agent_workspace_grants g JOIN api_keys k ON k.id=g.api_key_id WHERE g.workspace_id=? AND g.api_key_id=? AND k.is_active=1 AND k.status='active'", (row["workspace_id"], owner))).fetchone()
        if granted is None:
            raise error("workspace_not_found", 404)
        active = (await (await db.execute("SELECT count(*) FROM agent_jobs WHERE api_key_id=? AND state IN ('queued','running','awaiting_approval','applying')", (owner,))).fetchone())[0]
        if active >= settings.jobs_max_active_per_key:
            raise error("job_limit_exceeded", 429)
        if await (await db.execute("SELECT 1 FROM agent_jobs WHERE workspace_id=? AND state IN ('queued','running','awaiting_approval','applying')", (row["workspace_id"],))).fetchone():
            raise error("job_conflict")
        count = (await (await db.execute("SELECT count(*) FROM agent_job_resumptions WHERE job_id=?", (job_id,))).fetchone())[0]
        if count >= 100:
            raise error("job_resume_limit", 429)
        revision = data.get("revision", 0) + 1
        await _cancel_tests(db, job_id)
        data.update({"revision": revision, "instruction": instruction if instruction is not None else data["instruction"], "snapshot": None, "proposal": None, "approved_proposal_hash": None, "error_code": None, "route": {"provider": profile.provider, "model": profile.model, "reasoning_effort": profile.reasoning_effort}})
        await db.execute("UPDATE agent_jobs SET state='queued',profile_id=?,data_json=?,lease_until=NULL,worker_token=NULL,updated_at=? WHERE id=?", (profile.id, json.dumps(data), time.time(), job_id))
        await db.execute("INSERT INTO agent_job_resumptions VALUES (?,?,?,?,?)", (job_id, key_hash, fingerprint, revision, time.time()))
        await _event(db, job_id, "job.state", {"state": "queued", "reason": "user_resumed", "revision": revision})
        await db.commit()
    return await get_job(job_id, owner), True


async def _forget(db, job_id, owner=None):
    row = await (await db.execute("SELECT * FROM agent_jobs WHERE id=?" + (" AND api_key_id=?" if owner is not None else ""), (job_id, owner) if owner is not None else (job_id,))).fetchone()
    if row is None:
        raise error("job_not_found", 404)
    if row["state"] not in TERMINAL or json.loads(row["data_json"]).get("error_code") == "workspace_recovery_required":
        raise error("invalid_job_state")
    active_tests = await (await db.execute("SELECT 1 FROM agent_job_tests WHERE job_id=? AND state IN ('queued','running')", (job_id,))).fetchone()
    if active_tests:
        raise error("job_test_running")
    await db.execute("INSERT INTO agent_job_tombstones VALUES (?,?,?,?) ON CONFLICT(api_key_id,idempotency_hash) DO NOTHING", (row["api_key_id"], hashlib.sha256(row["idempotency_key"].encode()).hexdigest(), job_id, time.time()))
    await db.execute("DELETE FROM agent_job_events WHERE job_id=?", (job_id,))
    await db.execute("DELETE FROM agent_jobs WHERE id=?", (job_id,))


async def forget_job(job_id, owner):
    from app.job_tests import expire
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await expire(db)
        await _forget(db, job_id, owner)
        await db.commit()


async def prune_history():
    if not settings.jobs_retention_days:
        return 0
    deleted = 0
    from app.job_tests import expire
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await expire(db)
        rows = await (await db.execute("SELECT id FROM agent_jobs WHERE state IN ('completed','cancelled','failed') AND updated_at<? ORDER BY updated_at LIMIT 100", (time.time() - settings.jobs_retention_days * 86400,))).fetchall()
        for row in rows:
            try:
                await _forget(db, row["id"])
                deleted += 1
            except GatewayError as exc:
                if exc.code not in {"invalid_job_state", "job_test_running"}:
                    raise
        await db.commit()
    return deleted
