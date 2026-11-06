"""Durable test queue, claimed by an authenticated operator-run CLI executor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import job_store, workspaces
from app.config import settings
from app.database import _connect
from app.errors import GatewayError
from app.test_sandbox import MAX_OUTPUT, validate_files

router = APIRouter()
TERMINAL = {"completed", "failed", "cancelled", "timed_out", "interrupted"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_job_tests (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
 api_key_id INTEGER NOT NULL REFERENCES api_keys(id), idempotency_key TEXT NOT NULL,
 fingerprint TEXT NOT NULL, profile TEXT NOT NULL, proposal_hash TEXT NOT NULL,
 target TEXT NOT NULL, state TEXT NOT NULL, source_json TEXT NOT NULL,
 result_json TEXT, lease_hash TEXT, lease_until REAL, cancel_requested INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(api_key_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_job_tests_claim ON agent_job_tests(api_key_id,state,created_at);
"""


class TestCreate(BaseModel):
    profile: Literal["python-unittest", "python-compile"]
    proposal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: Literal["proposal", "applied"] = "proposal"
    model_config = ConfigDict(extra="forbid")


class Claim(BaseModel):
    profiles: list[Literal["python-unittest", "python-compile"]] = Field(min_length=1, max_length=2)
    model_config = ConfigDict(extra="forbid")


class Lease(BaseModel):
    lease_token: str = Field(min_length=32, max_length=128)
    model_config = ConfigDict(extra="forbid")


class TestResult(Lease):
    state: Literal["completed", "failed", "cancelled", "timed_out", "interrupted"]
    exit_code: int | None = Field(default=None, ge=-65535, le=65535)
    output: str = Field(default="", max_length=MAX_OUTPUT)
    output_truncated: bool = False
    error_code: str | None = Field(default=None, pattern=r"^[a-z_]{1,80}$")

    @model_validator(mode="after")
    def validate_result(self):
        if len(self.output.encode()) > MAX_OUTPUT or (self.state == "completed" and self.exit_code != 0):
            raise ValueError("Invalid test result")
        return self


def error(code, status=409):
    return GatewayError(status, "Operazione test non disponibile per questo lavoro o esecutore.", code=code)


async def init_tests():
    async with _connect() as db:
        await db.executescript(SCHEMA)


async def expire(db):
    rows = await (await db.execute("SELECT id,job_id FROM agent_job_tests WHERE state='running' AND lease_until<?", (time.time(),))).fetchall()
    for row in rows:
        await db.execute("UPDATE agent_job_tests SET state='interrupted',updated_at=? WHERE id=?", (time.time(), row["id"]))
        await job_store._event(db, row["job_id"], "job.test", {"test_id": row["id"], "state": "interrupted", "reason": "executor_lease_expired"})


def public(row):
    return {key: row[key] for key in ("id", "job_id", "profile", "proposal_hash", "target", "state", "created_at", "updated_at")} | {"cancel_requested": bool(row["cancel_requested"]), "result": json.loads(row["result_json"]) if row["result_json"] else None, "reported_by": "authenticated_executor"}


async def owned(db, test_id, owner):
    row = await (await db.execute("SELECT * FROM agent_job_tests WHERE id=? AND api_key_id=?", (test_id, owner))).fetchone()
    if row is None:
        raise error("job_test_not_found", 404)
    return row


@router.post("/v1/jobs/{job_id}/tests")
async def create_test(job_id: str, payload: TestCreate, request: Request, idempotency_key: str = Header(min_length=1, max_length=128)):
    owner = int(request.state.api_key_id)
    job = await job_store.get_job(job_id, owner)
    workspace = await job_store.get_workspace(job["workspace_id"], owner)
    if payload.profile not in workspace.get("test_profiles", []):
        raise error("test_profile_not_allowed", 403)
    fingerprint = hashlib.sha256(json.dumps([job_id, payload.model_dump()], sort_keys=True).encode()).hexdigest()
    async with _connect() as db:
        previous = await (await db.execute("SELECT * FROM agent_job_tests WHERE api_key_id=? AND idempotency_key=?", (owner, idempotency_key))).fetchone()
        if previous is not None:
            if previous["fingerprint"] != fingerprint:
                raise error("job_test_conflict")
            return JSONResponse(public(previous))
    proposal = job["data"].get("proposal") or {}
    if proposal.get("hash") != payload.proposal_hash or job["state"] != ("awaiting_approval" if payload.target == "proposal" else "completed"):
        raise error("invalid_job_state")
    base = job["data"]["snapshot"]
    source = {name: item["content"] for name, item in base.items() if item["content"] is not None}
    source.update({change["path"]: change["content"] for change in proposal["changes"]})
    try:
        validate_files(source)
    except (TypeError, ValueError) as exc:
        raise error("job_test_snapshot_limit", 400) from exc
    if payload.target == "applied":
        current = await asyncio.to_thread(workspaces.snapshot, workspace, job["data"]["paths"])
        if any(current[name]["content"] != source.get(name) for name in current):
            raise error("stale_workspace")
    now, test_id = time.time(), "test_" + uuid.uuid4().hex
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await expire(db)
        previous = await (await db.execute("SELECT * FROM agent_job_tests WHERE api_key_id=? AND idempotency_key=?", (owner, idempotency_key))).fetchone()
        if previous is not None:
            if previous["fingerprint"] != fingerprint:
                raise error("job_test_conflict")
            return JSONResponse(public(previous))
        active = await (await db.execute("SELECT COUNT(*) FROM agent_job_tests WHERE api_key_id=? AND state IN ('queued','running')", (owner,))).fetchone()
        if active[0] >= 2:
            raise error("job_test_limit_exceeded", 429)
        await db.execute("""INSERT INTO agent_job_tests
            (id,job_id,api_key_id,idempotency_key,fingerprint,profile,proposal_hash,target,state,source_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (test_id, job_id, owner, idempotency_key, fingerprint, payload.profile, payload.proposal_hash, payload.target, "queued", json.dumps(source), now, now))
        await job_store._event(db, job_id, "job.test", {"test_id": test_id, "state": "queued", "profile": payload.profile, "proposal_hash": payload.proposal_hash})
        await db.commit()
        result = public(await owned(db, test_id, owner))
    return JSONResponse(result, status_code=202)


@router.get("/v1/jobs/{job_id}/tests")
async def list_tests(job_id: str, request: Request):
    owner = int(request.state.api_key_id)
    await job_store.get_job(job_id, owner)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await expire(db)
        rows = await (await db.execute("SELECT * FROM agent_job_tests WHERE job_id=? AND api_key_id=? ORDER BY created_at DESC LIMIT 100", (job_id, owner))).fetchall()
        await db.commit()
    return {"data": [public(row) for row in rows]}


@router.post("/v1/job-tests/claim")
async def claim(payload: Claim, request: Request):
    owner = int(request.state.api_key_id)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await expire(db)
        rows = await (await db.execute("""SELECT t.*,w.config_json FROM agent_job_tests t
            JOIN agent_jobs j ON j.id=t.job_id JOIN agent_workspaces w ON w.id=j.workspace_id
            JOIN agent_workspace_grants g ON g.workspace_id=w.id AND g.api_key_id=t.api_key_id
            WHERE t.api_key_id=? AND t.state='queued' ORDER BY t.created_at LIMIT 100""", (owner,))).fetchall()
        row = next((row for row in rows if row["profile"] in payload.profiles and row["profile"] in json.loads(row["config_json"]).get("test_profiles", [])), None)
        if row is None:
            await db.commit()
            return Response(status_code=204)
        token = secrets.token_urlsafe(32)
        await db.execute("UPDATE agent_job_tests SET state='running',lease_hash=?,lease_until=?,updated_at=? WHERE id=?", (hashlib.sha256(token.encode()).hexdigest(), time.time() + settings.job_test_lease_seconds, time.time(), row["id"]))
        await job_store._event(db, row["job_id"], "job.test", {"test_id": row["id"], "state": "running"})
        await db.commit()
    return {"id": row["id"], "profile": row["profile"], "proposal_hash": row["proposal_hash"], "files": json.loads(row["source_json"]), "lease_token": token, "timeout_seconds": settings.job_test_timeout_seconds, "lease_seconds": settings.job_test_lease_seconds}


async def valid_lease(db, test_id, owner, token):
    row = await owned(db, test_id, owner)
    if row["state"] != "running" or row["lease_until"] < time.time() or not secrets.compare_digest(row["lease_hash"] or "", hashlib.sha256(token.encode()).hexdigest()):
        raise error("invalid_executor_lease")
    return row


@router.post("/v1/job-tests/{test_id}/heartbeat")
async def heartbeat(test_id: str, payload: Lease, request: Request):
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await valid_lease(db, test_id, int(request.state.api_key_id), payload.lease_token)
        await db.execute("UPDATE agent_job_tests SET lease_until=? WHERE id=?", (time.time() + settings.job_test_lease_seconds, test_id))
        await db.commit()
    return {"cancel_requested": bool(row["cancel_requested"])}


@router.post("/v1/job-tests/{test_id}/result")
async def result(test_id: str, payload: TestResult, request: Request):
    owner = int(request.state.api_key_id)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await owned(db, test_id, owner)
        serialized = json.dumps(payload.model_dump(exclude={"lease_token"}), sort_keys=True)
        if row["state"] in TERMINAL and secrets.compare_digest(row["lease_hash"] or "", hashlib.sha256(payload.lease_token.encode()).hexdigest()) and row["result_json"] == serialized:
            return public(row)
        await valid_lease(db, test_id, owner, payload.lease_token)
        await db.execute("UPDATE agent_job_tests SET state=?,result_json=?,updated_at=? WHERE id=?", (payload.state, serialized, time.time(), test_id))
        await job_store._event(db, row["job_id"], "job.test", {"test_id": test_id, "state": payload.state, "exit_code": payload.exit_code})
        await db.commit()
        return public(await owned(db, test_id, owner))


@router.post("/v1/job-tests/{test_id}/cancel")
async def cancel(test_id: str, request: Request):
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await owned(db, test_id, int(request.state.api_key_id))
        if row["state"] not in TERMINAL:
            state = "cancelled" if row["state"] == "queued" else row["state"]
            await db.execute("UPDATE agent_job_tests SET cancel_requested=1,state=?,updated_at=? WHERE id=?", (state, time.time(), test_id))
            await job_store._event(db, row["job_id"], "job.test", {"test_id": test_id, "state": state, "cancel_requested": True})
        await db.commit()
        return public(await owned(db, test_id, int(request.state.api_key_id)))
