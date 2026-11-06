from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app import job_store as store
from app import workspaces as files
from app.dashboard_security import require_dashboard_mutation
from app.database import get_gateway_api_for_key
from app.errors import GatewayError
from app.jobs import approve

router = APIRouter()


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    root: str = Field(min_length=1, max_length=2048)
    paths: list[str] = Field(min_length=1, max_length=files.MAX_FILES)
    api_key_ids: list[int] = Field(min_length=1, max_length=100)
    writable: bool = False
    test_profiles: list[Literal["python-unittest", "python-compile"]] = Field(default_factory=list, max_length=2)
    model_config = ConfigDict(extra="forbid")


class JobCreate(BaseModel):
    type: Literal["repo_patch"] = "repo_patch"
    workspace_id: str = Field(min_length=1, max_length=100)
    instruction: str = Field(min_length=1, max_length=16_000)
    paths: list[str] | None = Field(default=None, min_length=1, max_length=files.MAX_FILES)
    model_config = ConfigDict(extra="forbid")


class Approval(BaseModel):
    proposal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approve", "reject"]
    revised_instruction: str | None = Field(default=None, min_length=1, max_length=16_000)
    model_config = ConfigDict(extra="forbid")


class Resume(BaseModel):
    instruction: str | None = Field(default=None, min_length=1, max_length=16000)
    model_config = ConfigDict(extra="forbid")


def public(job: dict) -> dict:
    return {key: job[key] for key in ("id", "profile_id", "workspace_id", "state", "created_at", "updated_at")} | {key: job["data"].get(key) for key in ("type", "contract_version", "instruction", "paths", "proposal", "error_code", "job_profile")} | {"revision": job["data"].get("revision", 0)}


@router.post("/api/workspaces", dependencies=[Depends(require_dashboard_mutation)])
async def register_workspace(payload: WorkspaceCreate):
    config = await asyncio.to_thread(files.register_root, payload.root, payload.paths)
    config["test_profiles"] = list(dict.fromkeys(payload.test_profiles))
    workspace = await store.save_workspace(payload.name, config, payload.writable, payload.api_key_ids)
    return JSONResponse(status_code=201, content={key: workspace[key] for key in ("id", "name", "paths", "writable", "test_profiles")}, headers={"Cache-Control": "no-store"})


@router.get("/v1/workspaces")
async def workspaces(request: Request):
    return {"data": await store.list_workspaces(int(request.state.api_key_id))}


@router.post("/v1/jobs")
async def create_job(payload: JobCreate, request: Request, idempotency_key: str = Header(min_length=1, max_length=128)):
    return await create_job_for_request(payload, request, idempotency_key)


async def create_job_for_request(payload, request, idempotency_key, job_profile=None):
    owner = int(request.state.api_key_id)
    profile = await get_gateway_api_for_key(owner)
    if profile is None:
        raise GatewayError(400, "Usa una chiave associata a un profilo API.", code="job_profile_required")
    if profile.provider not in {"codex", "ollama", "openai_compatible"}:
        raise GatewayError(400, "Il provider non offre ancora l'analisi agente senza strumenti.", code="job_provider_not_supported")
    workspace = await store.get_workspace(payload.workspace_id, owner)
    paths = list(dict.fromkeys(payload.paths if payload.paths is not None else workspace["paths"]))
    if any(path not in workspace["paths"] for path in paths):
        raise files.failure("workspace_not_allowed", 403)
    job, created = await store.create_job(owner, profile, workspace, payload.instruction, paths, idempotency_key, job_profile=job_profile)
    return JSONResponse(status_code=202 if created else 200, content=public(job), headers={"Cache-Control": "no-store", "Location": f"/v1/jobs/{job['id']}"})


@router.get("/v1/jobs")
async def list_jobs(request: Request, limit: int = Query(default=50, ge=1, le=100), after: str | None = None, state: Literal["queued", "running", "awaiting_approval", "applying", "completed", "cancelled", "failed"] | None = None):
    return await store.list_jobs(int(request.state.api_key_id), limit, after, state)


@router.post("/v1/jobs/{job_id}/resume")
async def resume_job(job_id: str, payload: Resume, request: Request, idempotency_key: str = Header(min_length=1, max_length=128)):
    owner = int(request.state.api_key_id)
    job = await store.get_job(job_id, owner)
    workspace = await store.get_workspace(job["workspace_id"], owner)
    if any(path not in workspace["paths"] for path in job["data"]["paths"]):
        raise files.failure("workspace_not_allowed", 403)
    profile = await get_gateway_api_for_key(owner)
    if profile is None or profile.status != "active" or profile.provider not in {"codex", "ollama", "openai_compatible"}:
        raise store.error("job_profile_changed")
    resumed, changed = await store.resume_job(job_id, owner, profile, payload.instruction, idempotency_key)
    return JSONResponse(public(resumed), status_code=202 if changed else 200, headers={"Cache-Control": "no-store"})


@router.delete("/v1/jobs/{job_id}")
async def forget_job(job_id: str, request: Request):
    await store.forget_job(job_id, int(request.state.api_key_id))
    return {"id": job_id, "object": "job", "deleted": True}


@router.get("/v1/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    return public(await store.get_job(job_id, int(request.state.api_key_id)))


@router.post("/v1/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request):
    job = await store.get_job(job_id, int(request.state.api_key_id))
    if job["state"] == "cancelled":
        return public(job)
    changed = await store.transition(job_id, {"queued", "running", "awaiting_approval"}, "cancelled")
    if changed is None:
        raise store.error("invalid_job_state")
    request.app.state.job_runner.cancel(job_id)
    return public(changed)


@router.post("/v1/jobs/{job_id}/approvals")
async def approve_job(job_id: str, payload: Approval, request: Request):
    job = await store.get_job(job_id, int(request.state.api_key_id))
    if payload.decision == "approve":
        return public(await approve(job, payload.proposal_hash))
    if (job["data"].get("proposal") or {}).get("hash") != payload.proposal_hash:
        raise store.error("approval_required")
    state = "queued" if payload.revised_instruction else "cancelled"
    updates = {"proposal": None, "snapshot": None}
    if payload.revised_instruction:
        updates["instruction"] = payload.revised_instruction
        updates["revision"] = job["data"].get("revision", 0) + 1
    changed = await store.transition(job_id, {"awaiting_approval"}, state, updates, event_data={"reason": "proposal_rejected"})
    if changed is None:
        raise store.error("invalid_job_state")
    return public(changed)


@router.get("/v1/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, after: int = 0, follow: bool = True, last_event_id: str | None = Header(default=None)):
    owner = int(request.state.api_key_id)
    await store.get_job(job_id, owner)
    try:
        cursor = int(last_event_id) if last_event_id is not None else after
        if cursor < 0:
            raise ValueError()
    except ValueError as exc:
        raise GatewayError(400, "Cursore eventi non valido.", code="invalid_event_cursor") from exc

    async def events():
        nonlocal cursor
        while True:
            batch = await store.events(job_id, cursor)
            for event in batch:
                cursor = event["id"]
                yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            job = await store.get_job(job_id, owner)
            if len(batch) < 100 and (not follow or job["state"] in store.TERMINAL):
                return
            if not batch:
                yield ": keepalive\n\n"
                await asyncio.sleep(0.5)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
