"""Tenant administration. Global providers and tools remain operator-owned."""

import json
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.database import _connect, generate_api_key, hash_api_key
from app.identity import (
    COOKIE,
    audit,
    failure,
    principal,
    require_project,
    require_role,
)
from app.schemas import ApiLimits, GatewayApiConfig

router = APIRouter(prefix="/admin/v1", dependencies=[Depends(principal)])


class Named(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    model_config = ConfigDict(extra="forbid")


class Member(BaseModel):
    identity_id: str = Field(pattern=r"^usr_[0-9a-f]{32}$")
    role: Literal["owner", "admin", "developer", "viewer"]
    model_config = ConfigDict(extra="forbid")


class Credential(Named):
    profile_id: int = Field(ge=1)
    limits: ApiLimits = Field(default_factory=ApiLimits)


class Activation(BaseModel):
    active: bool
    model_config = ConfigDict(extra="forbid")


async def project_and_profile(db, actor, project_id, profile_id, role="viewer"):
    project = await require_project(db, actor, project_id, role)
    profile = await (await db.execute("SELECT 1 FROM profile_scopes WHERE profile_id=? AND project_id=? AND active=1", (profile_id, project_id))).fetchone()
    if profile is None:
        raise failure("profile_not_found", 404)
    return project


def request_id(request):
    # Do not trust client headers as arbitrary strings in the audit log.
    return "adm_" + uuid.uuid4().hex


@router.get("/me")
async def me(request: Request):
    actor = await principal(request)
    async with _connect() as db:
        rows = await (await db.execute("SELECT m.tenant_id,m.role,t.name FROM tenant_members m JOIN tenants t ON t.id=m.tenant_id WHERE m.identity_id=? AND t.active=1", (actor.id,))).fetchall()
    return {"id": actor.id, "name": actor.name, "operator": actor.operator, "session_id": actor.session_id, "expires_at": actor.expires_at, "tenants": [dict(row) for row in rows]}


@router.post("/session")
async def browser_session(request: Request):
    actor = await principal(request)
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise failure()
    response = JSONResponse({"identity_id": actor.id, "expires_at": actor.expires_at}, headers={"Cache-Control": "no-store"})
    response.set_cookie(COOKIE, token, httponly=True, secure=request.url.scheme == "https", samesite="strict", max_age=max(1, int(actor.expires_at - time.time())), path="/")
    return response


@router.delete("/sessions/{session_id}")
async def revoke_session(session_id: str, request: Request):
    actor = await principal(request)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        changed = await db.execute("UPDATE identity_sessions SET revoked=1 WHERE id=? AND identity_id=?", (session_id, actor.id))
        if changed.rowcount != 1:
            raise failure("session_not_found", 404)
        await audit(db, actor, None, "session.revoke", session_id, request_id(request))
        await db.commit()
    response = Response(status_code=204)
    if actor.session_id == session_id:
        response.delete_cookie(COOKIE, path="/")
    return response


@router.post("/tenants", status_code=201)
async def create_tenant(payload: Named, request: Request):
    actor = await principal(request)
    if not actor.operator:
        raise failure()
    if not settings.admin_auth_required:
        raise failure("team_authentication_required", 409)
    identifier = "ten_" + uuid.uuid4().hex
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("INSERT INTO tenants VALUES (?,?,1,?)", (identifier, payload.name, time.time()))
        await db.execute("INSERT INTO tenant_members VALUES (?,?,?)", (identifier, actor.id, "owner"))
        await audit(db, actor, identifier, "tenant.create", identifier, request_id(request))
        await db.commit()
    return {"id": identifier, "name": payload.name}


@router.patch("/tenants/{tenant_id}/projects/{project_id}")
async def activate_project(tenant_id: str, project_id: str, payload: Activation, request: Request):
    actor = await principal(request)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await require_role(db, actor, tenant_id, "admin")
        cursor = await db.execute("UPDATE team_projects SET active=? WHERE id=? AND tenant_id=?", (int(payload.active), project_id, tenant_id))
        if cursor.rowcount != 1:
            raise failure("project_not_found", 404)
        await audit(db, actor, tenant_id, "project.activate" if payload.active else "project.suspend", project_id, request_id(request))
        await db.commit()
    return {"id": project_id, "active": payload.active}


@router.patch("/projects/{project_id}/profiles/{profile_id}")
async def activate_profile(project_id: str, profile_id: int, payload: Activation, request: Request):
    actor = await principal(request)
    if not actor.operator:
        raise failure()
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        project = await require_project(db, actor, project_id, "admin")
        cursor = await db.execute("UPDATE profile_scopes SET active=? WHERE profile_id=? AND project_id=?", (int(payload.active), profile_id, project_id))
        if cursor.rowcount != 1:
            raise failure("profile_not_found", 404)
        await audit(db, actor, project["tenant_id"], "profile.activate" if payload.active else "profile.suspend", profile_id, request_id(request))
        await db.commit()
    return {"id": profile_id, "active": payload.active}


@router.get("/projects/{project_id}/diagnostics")
async def project_diagnostics(project_id: str, request: Request):
    actor = await principal(request)
    async with _connect() as db:
        await require_project(db, actor, project_id)
        providers = await (await db.execute("""SELECT u.routed_provider AS provider,count(*) AS requests,
          SUM(CASE WHEN u.status_code>=400 THEN 1 ELSE 0 END) AS errors,
          MAX(u.created_at) AS last_used_at
          FROM usage_logs u JOIN application_credentials c ON c.api_key_id=u.api_key_id
          JOIN profile_scopes s ON s.profile_id=c.profile_id WHERE s.project_id=? GROUP BY u.routed_provider""", (project_id,))).fetchall()
        jobs = await (await db.execute("SELECT j.state,count(*) AS count FROM agent_jobs j JOIN application_credentials c ON c.api_key_id=j.api_key_id JOIN profile_scopes s ON s.profile_id=c.profile_id WHERE s.project_id=? GROUP BY j.state", (project_id,))).fetchall()
    return {"storage": "postgresql" if settings.database_url else "sqlite", "providers": [dict(row) for row in providers], "jobs": [dict(row) for row in jobs], "tokens_estimated": True}


@router.get("/diagnostics")
async def instance_diagnostics(request: Request):
    actor = await principal(request)
    if not actor.operator:
        raise failure()
    from app import deployments
    async with _connect() as db:
        versions = await (await db.execute("SELECT component,version,applied_at FROM schema_versions ORDER BY component")).fetchall()
        jobs = await (await db.execute("SELECT state,count(*) AS count FROM agent_jobs GROUP BY state")).fetchall()
    return {"storage": "postgresql" if settings.database_url else "sqlite", "schema_versions": [dict(row) for row in versions], "jobs": [dict(row) for row in jobs], "telemetry_enabled": settings.otel_enabled, "deployments": [await deployments.observe(target) for target in deployments.configured().values()]}


@router.get("/audit")
async def instance_audit(request: Request, after: int = 0, limit: int = 100):
    actor = await principal(request)
    if not actor.operator:
        raise failure()
    if after < 0 or not 1 <= limit <= 500:
        raise failure("invalid_audit_cursor", 400)
    async with _connect() as db:
        rows = await (await db.execute("SELECT * FROM administration_audit WHERE seq>? ORDER BY seq LIMIT ?", (after, limit))).fetchall()
    return {"data": [dict(row) for row in rows]}


@router.get("/tenants/{tenant_id}/members")
async def members(tenant_id: str, request: Request):
    actor = await principal(request)
    async with _connect() as db:
        await require_role(db, actor, tenant_id, "admin")
        rows = await (await db.execute("SELECT m.identity_id,m.role,i.name,i.active FROM tenant_members m JOIN identities i ON i.id=m.identity_id WHERE m.tenant_id=?", (tenant_id,))).fetchall()
    return {"data": [dict(row) for row in rows]}


async def change_member(tenant_id, identity_id, role, request):
    actor = await principal(request)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        own_role = await require_role(db, actor, tenant_id, "admin")
        old = await (await db.execute("SELECT role FROM tenant_members WHERE tenant_id=? AND identity_id=?", (tenant_id, identity_id))).fetchone()
        if own_role != "owner" and (role in {"owner", "admin"} or old and old["role"] in {"owner", "admin"}):
            raise failure()
        if old and old["role"] == "owner" and role != "owner":
            owners = await (await db.execute("SELECT count(*) FROM tenant_members WHERE tenant_id=? AND role='owner'", (tenant_id,))).fetchone()
            if owners[0] <= 1:
                raise failure("last_tenant_owner", 409)
        exists = await (await db.execute("SELECT 1 FROM identities WHERE id=? AND active=1", (identity_id,))).fetchone()
        if exists is None:
            raise failure("identity_not_found", 404)
        if role is None:
            await db.execute("DELETE FROM tenant_members WHERE tenant_id=? AND identity_id=?", (tenant_id, identity_id))
        else:
            await db.execute("INSERT INTO tenant_members VALUES (?,?,?) ON CONFLICT(tenant_id,identity_id) DO UPDATE SET role=excluded.role", (tenant_id, identity_id, role))
        await audit(db, actor, tenant_id, "member.remove" if role is None else "member.set", identity_id, request_id(request))
        await db.commit()


@router.put("/tenants/{tenant_id}/members")
async def put_member(tenant_id: str, payload: Member, request: Request):
    await change_member(tenant_id, payload.identity_id, payload.role, request)
    return {"identity_id": payload.identity_id, "role": payload.role}


@router.delete("/tenants/{tenant_id}/members/{identity_id}", status_code=204)
async def remove_member(tenant_id: str, identity_id: str, request: Request):
    await change_member(tenant_id, identity_id, None, request)
    return Response(status_code=204)


@router.get("/tenants/{tenant_id}/projects")
async def projects(tenant_id: str, request: Request):
    actor = await principal(request)
    async with _connect() as db:
        await require_role(db, actor, tenant_id)
        rows = await (await db.execute("SELECT id,name,active FROM team_projects WHERE tenant_id=?", (tenant_id,))).fetchall()
    return {"data": [dict(row) for row in rows]}


@router.post("/tenants/{tenant_id}/projects", status_code=201)
async def create_project(tenant_id: str, payload: Named, request: Request):
    actor = await principal(request)
    identifier = "prj_" + uuid.uuid4().hex
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await require_role(db, actor, tenant_id, "admin")
        if await (await db.execute("SELECT 1 FROM team_projects WHERE tenant_id=? AND name=?", (tenant_id, payload.name))).fetchone():
            raise failure("project_name_conflict", 409)
        await db.execute("INSERT INTO team_projects VALUES (?,?,?,1,?)", (identifier, tenant_id, payload.name, time.time()))
        await audit(db, actor, tenant_id, "project.create", identifier, request_id(request))
        await db.commit()
    return {"id": identifier, "name": payload.name}


@router.get("/projects/{project_id}/profiles")
async def profiles(project_id: str, request: Request):
    actor = await principal(request)
    async with _connect() as db:
        await require_project(db, actor, project_id)
        rows = await (await db.execute("SELECT g.id,g.name,g.slug,g.provider,g.model,g.reasoning_effort,s.active FROM gateway_apis g JOIN profile_scopes s ON s.profile_id=g.id WHERE s.project_id=?", (project_id,))).fetchall()
    return {"data": [dict(row) for row in rows]}


@router.post("/projects/{project_id}/profiles", status_code=201)
async def create_profile(project_id: str, payload: GatewayApiConfig, request: Request):
    actor = await principal(request)
    # The operator publishes access to shared provider accounts. Tenant
    # administrators can issue and revoke project keys, not acquire new
    # provider/tool permissions by guessing global IDs.
    if not actor.operator:
        raise failure()
    from app.main import _validate_gateway_api_config
    invalid = await _validate_gateway_api_config(request.app, payload)
    if invalid is not None:
        return invalid
    from app.database import _replace_gateway_api_capabilities
    slug = "profile-" + uuid.uuid4().hex
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        project = await require_project(db, actor, project_id, "admin")
        anchor = await db.execute("INSERT INTO api_keys(name,key_hint,key_hash,is_active) VALUES (?,?,?,0)", (slug, "profile-anchor", hash_api_key(generate_api_key())))
        created = await db.execute("INSERT INTO gateway_apis(name,slug,api_key_id,provider,model,reasoning_effort) VALUES (?,?,?,?,?,?)", (payload.name[:40] + " [" + slug[-8:] + "]", slug, anchor.lastrowid, payload.provider, payload.model, payload.reasoning_effort))
        profile_id = int(created.lastrowid)
        await db.execute("INSERT INTO profile_scopes VALUES (?,?,1)", (profile_id, project_id))
        await _replace_gateway_api_capabilities(db, profile_id, skill_ids=payload.skill_ids, mcp_tools=payload.flattened_mcp_tools)
        await audit(db, actor, project["tenant_id"], "profile.publish", profile_id, request_id(request))
        await db.commit()
    return {"id": profile_id, "slug": slug, "name": payload.name}


@router.post("/projects/{project_id}/keys", status_code=201)
async def create_key(project_id: str, payload: Credential, request: Request):
    actor = await principal(request)
    secret = generate_api_key()
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        project = await project_and_profile(db, actor, project_id, payload.profile_id, "admin")
        cursor = await db.execute("INSERT INTO api_keys(name,key_hint,key_hash,limits_json) VALUES (?,?,?,?)", (payload.name, secret[:12] + "…" + secret[-4:], hash_api_key(secret), json.dumps(payload.limits.model_dump())))
        key_id = int(cursor.lastrowid)
        await db.execute("INSERT INTO application_credentials VALUES (?,?,?,?)", (key_id, payload.profile_id, actor.id, time.time()))
        await audit(db, actor, project["tenant_id"], "key.create", key_id, request_id(request))
        await db.commit()
    return JSONResponse({"id": key_id, "api_key": secret, "profile_id": payload.profile_id, "limits": payload.limits.model_dump()}, status_code=201, headers={"Cache-Control": "no-store"})


@router.get("/projects/{project_id}/keys")
async def keys(project_id: str, request: Request):
    actor = await principal(request)
    async with _connect() as db:
        await require_project(db, actor, project_id)
        rows = await (await db.execute("SELECT k.id,k.name,k.key_hint,k.status,k.is_active,k.limits_json,c.profile_id FROM api_keys k JOIN application_credentials c ON c.api_key_id=k.id JOIN profile_scopes s ON s.profile_id=c.profile_id WHERE s.project_id=?", (project_id,))).fetchall()
    return {"data": [{key: row[key] for key in ("id", "name", "key_hint", "status", "is_active", "profile_id")} | {"limits": json.loads(row["limits_json"])} for row in rows]}


@router.patch("/projects/{project_id}/keys/{key_id}/limits")
async def key_limits(project_id: str, key_id: int, payload: ApiLimits, request: Request):
    actor = await principal(request)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        project = await require_project(db, actor, project_id, "admin")
        cursor = await db.execute("UPDATE api_keys SET limits_json=? WHERE id=? AND id IN (SELECT c.api_key_id FROM application_credentials c JOIN profile_scopes s ON s.profile_id=c.profile_id WHERE s.project_id=?)", (json.dumps(payload.model_dump()), key_id, project_id))
        if cursor.rowcount != 1:
            raise failure("key_not_found", 404)
        await audit(db, actor, project["tenant_id"], "key.limits", key_id, request_id(request))
        await db.commit()
    return {"id": key_id, "limits": payload.model_dump()}


@router.delete("/projects/{project_id}/keys/{key_id}", status_code=204)
async def revoke_key(project_id: str, key_id: int, request: Request):
    actor = await principal(request)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        project = await require_project(db, actor, project_id, "admin")
        cursor = await db.execute("UPDATE api_keys SET is_active=0 WHERE id=? AND id IN (SELECT c.api_key_id FROM application_credentials c JOIN profile_scopes s ON s.profile_id=c.profile_id WHERE s.project_id=?)", (key_id, project_id))
        if cursor.rowcount != 1:
            raise failure("key_not_found", 404)
        await audit(db, actor, project["tenant_id"], "key.revoke", key_id, request_id(request))
        await db.commit()
    return Response(status_code=204)


@router.get("/tenants/{tenant_id}/audit")
async def audit_log(tenant_id: str, request: Request, after: int = 0, limit: int = 100):
    actor = await principal(request)
    if after < 0 or not 1 <= limit <= 500:
        raise failure("invalid_audit_cursor", 400)
    async with _connect() as db:
        await require_role(db, actor, tenant_id, "admin")
        rows = await (await db.execute("SELECT * FROM administration_audit WHERE tenant_id=? AND seq>? ORDER BY seq LIMIT ?", (tenant_id, after, limit))).fetchall()
    return {"data": [dict(row) for row in rows]}
