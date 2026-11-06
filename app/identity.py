"""Human identities and project scopes, separate from application credentials.

Provisioning is operator-local. No public registration or implicit adoption of
legacy keys is allowed when administrative authentication is enabled.
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.database import _connect, record_schema_version
from app.errors import GatewayError, openai_error_body

SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 is_operator INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_sessions (
 id TEXT PRIMARY KEY, identity_id TEXT NOT NULL REFERENCES identities(id),
 token_hash TEXT NOT NULL UNIQUE, expires_at REAL NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tenants (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tenant_members (
 tenant_id TEXT NOT NULL REFERENCES tenants(id),
 identity_id TEXT NOT NULL REFERENCES identities(id),
 role TEXT NOT NULL CHECK(role IN ('owner','admin','developer','viewer')),
 PRIMARY KEY(tenant_id,identity_id)
);
CREATE TABLE IF NOT EXISTS team_projects (
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
 UNIQUE(tenant_id,name)
);
CREATE TABLE IF NOT EXISTS profile_scopes (
 profile_id INTEGER PRIMARY KEY REFERENCES gateway_apis(id),
 project_id TEXT NOT NULL REFERENCES team_projects(id),
 active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS application_credentials (
 api_key_id INTEGER PRIMARY KEY REFERENCES api_keys(id),
 profile_id INTEGER NOT NULL REFERENCES profile_scopes(profile_id),
 created_by TEXT NOT NULL REFERENCES identities(id), created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS administration_audit (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, identity_id TEXT,
 tenant_id TEXT, operation TEXT NOT NULL, target TEXT NOT NULL,
 outcome TEXT NOT NULL, request_id TEXT NOT NULL, created_at REAL NOT NULL
);
"""
COOKIE = "omniproxy_admin"
ROLES = {"viewer": 0, "developer": 1, "admin": 2, "owner": 3}


async def init_identity():
    async with _connect() as db:
        await db.executescript(SCHEMA)
        await record_schema_version(db, "identity", 1)


async def check_auth_mode():
    if not settings.admin_auth_required:
        async with _connect() as db:
            if await (await db.execute("SELECT 1 FROM tenants LIMIT 1")).fetchone():
                raise RuntimeError("Team data requires ADMIN_AUTH_REQUIRED=true; administrative authentication cannot be disabled by restarting in personal mode")


def failure(code="admin_forbidden", status=403):
    return GatewayError(status, "Accesso amministrativo non autorizzato.", code=code)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    id: str
    name: str
    operator: bool
    session_id: str
    expires_at: float


async def authenticate(token) -> Principal | None:
    if not token.startswith("omni-admin-") or len(token) > 256:
        return None
    async with _connect() as db:
        cursor = await db.execute("""SELECT i.*,s.id AS session_id,s.expires_at
          FROM identities i JOIN identity_sessions s ON s.identity_id=i.id
          WHERE s.token_hash=? AND s.revoked=0 AND s.expires_at>? AND i.active=1""", (digest(token), time.time()))
        row = await cursor.fetchone()
    return Principal(row["id"], row["name"], bool(row["is_operator"]), row["session_id"], row["expires_at"]) if row else None


async def principal(request: Request) -> Principal:
    existing = getattr(request.state, "principal", None)
    if existing:
        return existing
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    value = await authenticate(token) if scheme.lower() == "bearer" else None
    if value is None:
        raise failure("admin_authentication_required", 401)
    request.state.principal = value
    return value


async def require_role(db, actor: Principal, tenant: str, minimum="viewer"):
    # Recheck identity/session inside the write transaction, preventing a
    # concurrent revocation from being bypassed with a stale middleware result.
    session = await db.execute("SELECT 1 FROM identity_sessions s JOIN identities i ON i.id=s.identity_id WHERE s.id=? AND s.identity_id=? AND s.revoked=0 AND s.expires_at>? AND i.active=1", (actor.session_id, actor.id, time.time()))
    if await session.fetchone() is None:
        raise failure("admin_authentication_required", 401)
    cursor = await db.execute("SELECT m.role FROM tenant_members m JOIN tenants t ON t.id=m.tenant_id WHERE m.tenant_id=? AND m.identity_id=? AND t.active=1", (tenant, actor.id))
    row = await cursor.fetchone()
    if row is None or ROLES[row["role"]] < ROLES[minimum]:
        raise failure()
    return row["role"]


async def require_project(db, actor, project, minimum="viewer"):
    row = await (await db.execute("SELECT * FROM team_projects WHERE id=? AND active=1", (project,))).fetchone()
    if row is None:
        raise failure("project_not_found", 404)
    await require_role(db, actor, row["tenant_id"], minimum)
    return row


async def audit(db, actor, tenant, operation, target, request_id, outcome="success"):
    # Metadata only: no names, request bodies, headers, keys, prompts or results.
    await db.execute("INSERT INTO administration_audit(identity_id,tenant_id,operation,target,outcome,request_id,created_at) VALUES (?,?,?,?,?,?,?)", (actor.id if actor else None, tenant, operation, str(target), outcome, request_id, time.time()))


async def provision(name: str, *, operator=False, tenant=None, role="owner", hours=24):
    """Only invoked by the local operator CLI and isolated tests."""
    if not name.strip() or len(name) > 100 or role not in ROLES or not 1 <= hours <= 168:
        raise ValueError("Invalid identity provisioning parameters")
    identity, session, token = "usr_" + uuid.uuid4().hex, "ses_" + uuid.uuid4().hex, "omni-admin-" + secrets.token_urlsafe(40)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("INSERT INTO identities VALUES (?,?,1,?,?)", (identity, name, int(operator), time.time()))
        if tenant:
            row = await (await db.execute("SELECT id FROM tenants WHERE id=? AND active=1", (tenant,))).fetchone()
            if row is None:
                raise ValueError("Tenant not found")
            await db.execute("INSERT INTO tenant_members VALUES (?,?,?)", (tenant, identity, role))
        await db.execute("INSERT INTO identity_sessions VALUES (?,?,?,?,0,?)", (session, identity, digest(token), time.time() + hours * 3600, time.time()))
        await audit(db, None, tenant, "identity.provision", identity, "operator-cli")
        await db.commit()
    return {"identity_id": identity, "session_id": session, "token": token}


async def application_key_allowed(key_id):
    async with _connect() as db:
        row = await (await db.execute("""SELECT s.active AS profile_active,p.active AS project_active,t.active AS tenant_active
          FROM application_credentials c JOIN profile_scopes s ON s.profile_id=c.profile_id
          JOIN team_projects p ON p.id=s.project_id JOIN tenants t ON t.id=p.tenant_id
          WHERE c.api_key_id=?""", (key_id,))).fetchone()
    return all(row[name] for name in ("profile_active", "project_active", "tenant_active")) if row else not settings.admin_auth_required


async def renew_session(identity_id: str, hours=24):
    if not 1 <= hours <= 168:
        raise ValueError("Session duration must be 1–168 hours")
    session, token = "ses_" + uuid.uuid4().hex, "omni-admin-" + secrets.token_urlsafe(40)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        if await (await db.execute("SELECT 1 FROM identities WHERE id=? AND active=1", (identity_id,))).fetchone() is None:
            raise ValueError("Active identity not found")
        await db.execute("INSERT INTO identity_sessions VALUES (?,?,?,?,0,?)", (session, identity_id, digest(token), time.time() + hours * 3600, time.time()))
        await audit(db, None, None, "session.issue", identity_id, "operator-cli")
        await db.commit()
    return {"identity_id": identity_id, "session_id": session, "token": token}


def session_anniversary(now: datetime, years: int):
    if type(years) is not int or not 1 <= years <= 5 or now.tzinfo is None:
        raise ValueError("Use an explicit duration of 1–5 calendar years")
    try:
        return now.replace(year=now.year + years)
    except ValueError:
        return now.replace(year=now.year + years, day=28)


async def extend_operator_session(session_id: str, token: str, *, years: int):
    """Local CLI only: explicit long duration, preserving the existing credential."""
    now = time.time()
    expires = session_anniversary(datetime.fromtimestamp(now, timezone.utc), years).timestamp()
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute("""SELECT i.id,s.expires_at FROM identities i
          JOIN identity_sessions s ON s.identity_id=i.id
          WHERE s.id=? AND s.token_hash=? AND s.revoked=0 AND s.expires_at>?
          AND i.active=1 AND i.is_operator=1""", (session_id, digest(token), now))).fetchone()
        if row is None:
            raise ValueError("A matching active operator session is required")
        if expires <= row["expires_at"]:
            raise ValueError("The requested duration would not extend this session")
        await db.execute("UPDATE identity_sessions SET expires_at=? WHERE id=?", (expires, session_id))
        await audit(db, None, None, "session.extend.local", session_id, "operator-cli")
        await db.commit()
    return {"identity_id": row["id"], "session_id": session_id, "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(), "credential_preserved": True}


class AdministrativeAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        protected = path == "/admin" or path.startswith("/admin/v1/") or ((settings.admin_auth_required or settings.dashboard_auth_required) and (path in {"/", "/readyz"} or path.startswith("/api/")))
        if not protected or request.method == "OPTIONS":
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        bearer = scheme.lower() == "bearer" and bool(token)
        if not bearer:
            token = request.cookies.get(COOKIE, "")
        try:
            actor = await authenticate(token)
        except Exception:  # noqa: BLE001 -- fail closed without storage diagnostics
            return JSONResponse(openai_error_body("Archivio identità non disponibile.", error_type="service_unavailable", code="admin_storage_unavailable"), status_code=503)
        if actor is None:
            if path in {"/", "/admin"} and not bearer:
                return RedirectResponse("/admin/login", status_code=303)
            return JSONResponse(openai_error_body("Autenticazione amministrativa richiesta.", error_type="invalid_request_error", code="admin_authentication_required"), status_code=401, headers={"WWW-Authenticate": "Bearer"})
        if path != "/admin" and not path.startswith("/admin/v1/") and not actor.operator:
            return JSONResponse(openai_error_body("Operazione riservata all'operatore dell'istanza.", error_type="invalid_request_error", code="admin_forbidden"), status_code=403)
        if not bearer and request.method not in {"GET", "HEAD", "OPTIONS"}:
            from app.dashboard_security import require_dashboard_mutation
            try:
                require_dashboard_mutation(request)
            except HTTPException:
                return JSONResponse(openai_error_body("Origin non autorizzata.", error_type="invalid_request_error", code="admin_csrf_rejected"), status_code=403)
        request.state.principal = actor
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (path.startswith("/api/") or response.status_code >= 400):
            # Successful tenant mutations audit atomically in their transaction.
            # Legacy global actions and rejected authorized requests are covered
            # here, using route templates rather than supplied URL values.
            try:
                async with _connect() as db:
                    await audit(db, actor, None, "http." + request.method, getattr(request.scope.get("route"), "path", "administration"), "adm_" + uuid.uuid4().hex, "success" if response.status_code < 400 else "rejected")
                    await db.commit()
            except Exception:  # noqa: BLE001 -- original mutation may already be committed
                import logging
                logging.getLogger(__name__).warning("Unable to persist administrative request audit")
        return response
