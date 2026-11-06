"""Immutable, owner-scoped recipes for repeatable repository jobs."""

import hashlib
import json
import time
import uuid

from fastapi import APIRouter, Header, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app import job_store
from app.database import _connect
from app.errors import GatewayError
from app.job_api import JobCreate, create_job_for_request

router = APIRouter()


class JobProfile(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    workspace_id: str = Field(min_length=1, max_length=100)
    instruction: str = Field(min_length=1, max_length=12000)
    paths: list[str] | None = Field(default=None, min_length=1, max_length=50)
    model_config = ConfigDict(extra="forbid")


class RunProfile(BaseModel):
    instruction: str | None = Field(default=None, min_length=1, max_length=4000)
    model_config = ConfigDict(extra="forbid")


def public(row):
    return {"id": row["id"], "name": row["name"], "workspace_id": row["workspace_id"], "fingerprint": row["fingerprint"], "created_at": row["created_at"], **json.loads(row["config_json"])}


async def owned(profile_id, owner):
    async with _connect() as db:
        row = await (await db.execute("SELECT * FROM agent_job_profiles WHERE id=? AND api_key_id=?", (profile_id, owner))).fetchone()
    if row is None:
        raise GatewayError(404, "Profilo lavoro non trovato.", code="job_profile_not_found")
    return row


@router.post("/v1/job-profiles", status_code=201)
async def create_profile(payload: JobProfile, request: Request):
    owner = int(request.state.api_key_id)
    workspace = await job_store.get_workspace(payload.workspace_id, owner)
    paths = list(dict.fromkeys(payload.paths or workspace["paths"]))
    if any(path not in workspace["paths"] for path in paths):
        raise GatewayError(403, "Percorsi non autorizzati.", code="workspace_not_allowed")
    config = {"type": "repo_patch", "contract_version": "1", "instruction": payload.instruction, "paths": paths}
    serialized = json.dumps(config, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(json.dumps([payload.workspace_id, config], sort_keys=True).encode()).hexdigest()
    profile_id = "jprof_" + uuid.uuid4().hex
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        if await (await db.execute("SELECT 1 FROM agent_workspace_grants WHERE workspace_id=? AND api_key_id=?", (payload.workspace_id, owner))).fetchone() is None:
            raise job_store.error("workspace_not_found", 404)
        count = (await (await db.execute("SELECT count(*) FROM agent_job_profiles WHERE api_key_id=?", (owner,))).fetchone())[0]
        if count >= 100:
            raise GatewayError(429, "Limite profili lavoro raggiunto.", code="job_profile_limit")
        await db.execute("INSERT INTO agent_job_profiles VALUES (?,?,?,?,?,?,?)", (profile_id, owner, payload.workspace_id, payload.name, serialized, fingerprint, time.time()))
        await db.commit()
    return public(await owned(profile_id, owner))


@router.get("/v1/job-profiles")
async def list_profiles(request: Request):
    async with _connect() as db:
        rows = await (await db.execute("SELECT * FROM agent_job_profiles WHERE api_key_id=? ORDER BY created_at DESC LIMIT 100", (int(request.state.api_key_id),))).fetchall()
    return {"data": [public(row) for row in rows]}


@router.get("/v1/job-profiles/{profile_id}")
async def get_profile(profile_id: str, request: Request):
    return public(await owned(profile_id, int(request.state.api_key_id)))


@router.delete("/v1/job-profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: str, request: Request):
    async with _connect() as db:
        changed = await db.execute("DELETE FROM agent_job_profiles WHERE id=? AND api_key_id=?", (profile_id, int(request.state.api_key_id)))
        if changed.rowcount != 1:
            raise GatewayError(404, "Profilo lavoro non trovato.", code="job_profile_not_found")
        await db.commit()
    return Response(status_code=204)


@router.post("/v1/job-profiles/{profile_id}/jobs")
async def run_profile(profile_id: str, payload: RunProfile, request: Request, idempotency_key: str = Header(min_length=1, max_length=128)):
    record = public(await owned(profile_id, int(request.state.api_key_id)))
    instruction = record["instruction"] + ("\n\nAdditional task:\n" + payload.instruction if payload.instruction else "")
    if len(instruction) > 16000:
        raise GatewayError(400, "Istruzione lavoro troppo lunga.", code="job_instruction_limit")
    job = JobCreate(workspace_id=record["workspace_id"], instruction=instruction, paths=record["paths"])
    return await create_job_for_request(job, request, idempotency_key, {"id": record["id"], "fingerprint": record["fingerprint"]})
