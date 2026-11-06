"""Disposable PostgreSQL integration checks. No host ports or persistent data."""

import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import httpx

from app import job_store, shared_capacity, token_budget
from app.citations import Citation, init_citations
from app.citations import verify as verify_citation
from app.config import settings
from app.database import (
    _connect,
    authenticate_api_key,
    create_gateway_api,
    init_database,
    record_usage,
)
from app.file_uploads import init_files
from app.identity import authenticate, provision
from app.job_tests import init_tests
from app.schemas import ApiLimits
from scripts.migrate_postgres import file_digest, migrate


async def verify_team_http(profile, identity):
    from app.main import app
    original_mode, original_bootstrap = settings.admin_auth_required, settings.bootstrap_api_key
    settings.admin_auth_required, settings.bootstrap_api_key = True, ""
    try:
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            admin = {"Authorization": "Bearer " + identity["token"]}
            dashboard = {**admin, "Origin": "http://127.0.0.1", "X-OmniProxy-Request": "dashboard"}
            group = await client.post("/api/gateway-apis/groups", headers=dashboard, json={"name": "HTTP group"})
            assert group.status_code == 201, group.status_code
            group_id = group.json()["id"]
            assert (await client.put(f"/api/gateway-apis/{profile.id}/group", headers=dashboard, json={"group_id": group_id})).status_code == 200
            assert (await client.patch(f"/api/gateway-apis/groups/{group_id}", headers=dashboard, json={"collapsed": True})).status_code == 200
            listed = (await client.get("/api/gateway-apis/groups", headers=admin)).json()["data"]
            assert next(item for item in listed if item["id"] == group_id)["api_ids"] == [profile.id]
            assert (await client.delete(f"/api/gateway-apis/groups/{group_id}", headers=dashboard)).status_code == 200
            tenant = await client.post("/admin/v1/tenants", headers=admin, json={"name": "PostgreSQL HTTP team"})
            assert tenant.status_code == 201, tenant.status_code
            project = await client.post(f"/admin/v1/tenants/{tenant.json()['id']}/projects", headers=admin, json={"name": "HTTP project"})
            assert project.status_code == 201, project.status_code
            project_id = project.json()["id"]
            async with _connect() as db:
                await db.execute("INSERT INTO profile_scopes VALUES (?,?,1)", (profile.id, project_id))
                await db.commit()
            minted = await client.post(f"/admin/v1/projects/{project_id}/keys", headers=admin, json={"name": "HTTP application", "profile_id": profile.id, "limits": {"token_budget": 1000, "budget_period": "daily"}})
            assert minted.status_code == 201, minted.status_code
            key = minted.json()
            auth = {"Authorization": "Bearer " + key["api_key"]}
            assert (await client.get("/v1/models", headers=auth)).json()["data"][0]["id"] == profile.slug
            await record_usage(request_id="http-fixture-usage", api_key_id=key["id"], requested_model=profile.slug, routed_provider="ollama", resolved_model="fixture", prompt_tokens=2, completion_tokens=2, status_code=200, latency_ms=1)
            usage = await client.get("/v1/usage", headers=auth)
            assert usage.status_code == 200 and usage.json()["usage"]["used_tokens"] == 4
            for path in ("/admin/v1/diagnostics", f"/admin/v1/projects/{project_id}/diagnostics", "/api/usage"):
                response = await client.get(path, headers=admin)
                assert response.status_code == 200, (path, response.status_code)
                assert key["api_key"] not in response.text and identity["token"] not in response.text
            assert (await client.delete(f"/admin/v1/projects/{project_id}/keys/{key['id']}", headers=admin)).status_code == 204
            assert (await client.get("/v1/models", headers=auth)).status_code == 401
    finally:
        settings.admin_auth_required, settings.bootstrap_api_key = original_mode, original_bootstrap

IMAGE = "postgres:17.11-bookworm@sha256:665dc7b37ac52a17e298511414c21d23ffa4e6a6ef6225b3e5d59c7afb35e994"


def docker(*args, **kwargs):
    result = subprocess.run(["docker", *args], check=True, capture_output=True, text=True, **kwargs)
    return result.stdout.strip()


async def verify_migration_restore(container, url):
    with tempfile.TemporaryDirectory(prefix="omniproxy-migration-") as temporary:
        original_url, original_path = settings.database_url, settings.database_path
        try:
            settings.database_url = ""
            settings.database_path = Path(temporary) / "snapshot.sqlite3"
            await init_database()
            await job_store.init_jobs()
            await init_tests()
            await init_files()
            await init_citations()
            profile, key = await create_gateway_api(name="Snapshot fixture", provider="ollama", model="fixture", reasoning_effort="off")
            identity = await provision("Snapshot identity", operator=True)
            workspace = await job_store.save_workspace("Snapshot workspace", {"paths": ["a.py"]}, True, [profile.api_key_id])
            job, _ = await job_store.create_job(profile.api_key_id, profile, workspace, "Fixture task", ["a.py"], "snapshot-job")
            await job_store.transition(job["id"], {"queued"}, "cancelled")
            await job_store.resume_job(job["id"], profile.api_key_id, profile, None, "snapshot-resume")
            await job_store.transition(job["id"], {"queued"}, "awaiting_approval", {"fixture": "persisted"})
            other_workspace = await job_store.save_workspace("Deleted fixture", {"paths": ["a.py"]}, False, [profile.api_key_id])
            deleted, _ = await job_store.create_job(profile.api_key_id, profile, other_workspace, "Deleted fixture", ["a.py"], "deleted-fixture")
            await job_store.transition(deleted["id"], {"queued"}, "cancelled")
            await job_store.forget_job(deleted["id"], profile.api_key_id)
            file_id, citation_id, recipe_id = "file-" + uuid.uuid4().hex, "cite_" + uuid.uuid4().hex, "jprof_" + uuid.uuid4().hex
            checksum = hashlib.sha256(b"BLUE").hexdigest()
            locator = {"file_id": file_id, "unit_id": "document-1", "mode": "auto", "source_sha256": checksum, "text_sha256": checksum, "start": 0, "end": 4}
            async with _connect() as db:
                await db.execute("INSERT INTO uploaded_files VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (file_id, profile.api_key_id, "a.txt", "text/plain", "text", "assistants", 4, checksum, b"BLUE", "{}", 1, 253402300799))
                parsed = json.dumps({"parts": [{"type": "text", "text": "BLUE"}], "metadata": {"kind": "text"}, "warnings": []})
                await db.execute("INSERT INTO file_extractions VALUES (?,?,?,?)", (file_id, "auto", parsed, len(parsed)))
                serialized = json.dumps(locator, sort_keys=True)
                await db.execute("INSERT INTO document_citations VALUES (?,?,?,?,?,?)", (citation_id, profile.api_key_id, file_id, hashlib.sha256(serialized.encode()).hexdigest(), serialized, 1))
                await db.execute("INSERT INTO agent_job_profiles VALUES (?,?,?,?,?,?,?)", (recipe_id, profile.api_key_id, workspace["id"], "Fixture recipe", json.dumps({"instruction": "Generic task", "paths": ["a.py"]}), "fixture-profile-hash", 1))
                await db.execute("INSERT INTO dashboard_api_groups VALUES (?,?,1)", ("grp_migration_fixture", "Studio"))
                await db.execute("INSERT INTO dashboard_api_group_members VALUES (?,?)", (profile.id, "grp_migration_fixture"))
                await db.commit()
            fingerprint = file_digest(settings.database_path)
            docker("exec", container, "createdb", "-U", "fixture", "--owner", "omniproxy_runtime", "migration")
            settings.database_url = url.rsplit("/", 1)[0] + "/migration"
            report = await migrate(settings.database_path, fingerprint)
            assert report["verified"] and report["rows"]["uploaded_files"] == 1
            assert (await authenticate_api_key(key)).id == profile.api_key_id
            assert (await authenticate(identity["token"])).operator
            assert (await job_store.get_job(job["id"]))["state"] == "awaiting_approval"
            try:
                await migrate(settings.database_path, fingerprint)
                raise AssertionError("Nonempty target was accepted")
            except ValueError as exc:
                assert "empty" in str(exc)
            created, _ = await create_gateway_api(name="After migration", provider="ollama", model="fixture", reasoning_effort="off")
            assert created.api_key_id > profile.api_key_id
            backup = (await asyncio.to_thread(subprocess.run, ["docker", "exec", container, "pg_dump", "-U", "fixture", "-d", "migration", "--format=custom"], check=True, capture_output=True)).stdout
            docker("exec", container, "createdb", "-U", "fixture", "--owner", "omniproxy_runtime", "restored")
            await asyncio.to_thread(subprocess.run, ["docker", "exec", "-i", container, "pg_restore", "-U", "fixture", "--role", "omniproxy_runtime", "--no-owner", "-d", "restored"], input=backup, check=True, capture_output=True)
            settings.database_url = url.rsplit("/", 1)[0] + "/restored"
            await init_database()
            await job_store.init_jobs()
            assert await authenticate_api_key(key)
            assert await authenticate(identity["token"])
            assert (await job_store.get_job(job["id"]))["data"]["fixture"] == "persisted"
            assert (await job_store.get_job(job["id"]))["data"]["revision"] == 1
            assert await job_store.events(job["id"], 0)
            assert (await verify_citation(None, profile.api_key_id, Citation(**locator, quote="BLUE")))["valid"]
            async with _connect() as db:
                assert (await (await db.execute("SELECT name FROM agent_job_profiles WHERE id=?", (recipe_id,))).fetchone())[0] == "Fixture recipe"
                assert (await (await db.execute("SELECT count(*) FROM agent_job_tombstones")).fetchone())[0] == 1
                assert (await (await db.execute("SELECT collapsed FROM dashboard_api_groups WHERE id=?", ("grp_migration_fixture",))).fetchone())[0] == 1
                assert (await (await db.execute("SELECT group_id FROM dashboard_api_group_members WHERE gateway_api_id=?", (profile.id,))).fetchone())[0] == "grp_migration_fixture"
        finally:
            settings.database_url, settings.database_path = original_url, original_path


async def multiprocess_claims():
    code = "import asyncio,json;from app.job_store import claim;job=asyncio.run(claim());print(json.dumps({'id':job['id'] if job else None}))"
    env = {**os.environ, "DATABASE_URL": settings.database_url}
    async def claim():
        child = await asyncio.create_subprocess_exec(sys.executable, "-c", code, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        output, _ = await child.communicate()
        assert child.returncode == 0, "Claim subprocess failed"
        return json.loads(output)["id"]
    return await asyncio.gather(*(claim() for _ in range(8)))


async def verify_shared_capacity():
    from app.deployments import Deployment
    target = Deployment(id="replica-probe", base_url="http://fixture/v1", model="fixture", max_concurrency=1)
    code = """import asyncio,json
from app.shared_capacity import acquire
from app.deployments import Deployment
async def run():
 target=Deployment(id='replica-probe',base_url='http://fixture/v1',model='fixture',max_concurrency=1)
 try: await acquire(target,{target.id:target});print('acquired')
 except Exception as e: print(getattr(e,'code','unexpected'))
asyncio.run(run())
"""
    async def acquire():
        child = await asyncio.create_subprocess_exec(sys.executable, "-c", code, env={**os.environ, "DATABASE_URL": settings.database_url}, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        output, _ = await child.communicate()
        assert child.returncode == 0
        return output.decode().strip()
    outcomes = await asyncio.gather(*(acquire() for _ in range(8)))
    assert outcomes.count("acquired") == 1 and outcomes.count("deployment_busy") == 7, outcomes
    assert (await shared_capacity.diagnostics(target))["active"] == 1
    async with _connect() as db:
        await db.execute("UPDATE upstream_capacity_leases SET expires_at=1")
        await db.commit()
    lease = await shared_capacity.acquire(target, {target.id: target})
    await shared_capacity.release(lease)
    await shared_capacity.failed(target, "upstream_quota_exceeded", 10)
    assert await acquire() == "upstream_quota_exceeded"


async def verify():
    password, application_password = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
    container = docker("run", "-d", "--rm", "--network", "bridge", "--user", "999:999", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "512m", "--pids-limit", "128", "--tmpfs", "/var/lib/postgresql/data:rw,nosuid,nodev,uid=999,gid=999,size=256m", "--tmpfs", "/var/run/postgresql:rw,nosuid,nodev,uid=999,gid=999", "-e", "POSTGRES_PASSWORD", "-e", "OMNI_APP_PASSWORD", "-e", "POSTGRES_USER=fixture", "-e", "POSTGRES_DB=fixture", IMAGE, env={**os.environ, "POSTGRES_PASSWORD": password, "OMNI_APP_PASSWORD": application_password})
    original = settings.database_url
    try:
        address = json.loads(docker("inspect", container))[0]["NetworkSettings"]["Networks"]["bridge"]["IPAddress"]
        settings.database_url = f"postgresql://fixture:{password}@{address}:5432/fixture"
        from app.storage_postgres import connect
        for attempt in range(100):
            try:
                probe = await connect(settings.database_url)
                await probe.close()
                break
            except Exception:
                if attempt == 99:
                    raise
                await asyncio.sleep(0.2)
        docker("exec", "-i", container, "sh", input=Path("deploy/postgres-init.sh").read_text())
        settings.database_url = f"postgresql://omniproxy_runtime:{application_password}@{address}:5432/omniproxy"
        for _ in range(2):
            await init_database()
            await job_store.init_jobs()
            await init_tests()
            await init_files()
            await shared_capacity.init_capacity()
        async with _connect() as db:
            role = await (await db.execute("SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user")).fetchone()
            assert not any(role.values)
        identity = await provision("PostgreSQL fixture", operator=True)
        assert (await authenticate(identity["token"])).operator
        profile, _ = await create_gateway_api(name="PostgreSQL fixture", provider="ollama", model="fixture", reasoning_effort="off", limits={"token_budget": 100, "budget_period": "daily"})
        limits = ApiLimits(token_budget=100, budget_period="daily")

        async def reserve(index):
            try:
                await token_budget.reserve(f"probe-{index}", profile.api_key_id, limits, 10, 20)
                return True
            except Exception as exc:  # noqa: BLE001 -- assertion below checks the exact refusal
                assert getattr(exc, "code", None) == "token_budget_exceeded", type(exc).__name__
                return False
        outcomes = await asyncio.gather(*(reserve(index) for index in range(12)))
        assert sum(outcomes) == 3, outcomes
        status = await token_budget.status(profile.api_key_id)
        assert status["reserved_tokens"] == 90 and status["remaining_tokens"] == 10, status
        await token_budget.settle("probe-0", 15)
        assert (await token_budget.status(profile.api_key_id))["used_tokens"] == 15
        with tempfile.TemporaryDirectory(prefix="omniproxy-pg-workspace-") as temporary:
            workspace = await job_store.save_workspace("Fixture", {"root": str(Path(temporary)), "paths": ["a.py"]}, False, [profile.api_key_id])
            job, created = await job_store.create_job(profile.api_key_id, profile, workspace, "Explain the file", ["a.py"], "same-key")
            assert created
            replay, created = await job_store.create_job(profile.api_key_id, profile, workspace, "Explain the file", ["a.py"], "same-key")
            assert not created and replay["id"] == job["id"]
            claims = await asyncio.gather(*(job_store.claim() for _ in range(8)))
            assert sum(item is not None for item in claims) == 1
            assert await job_store.events(job["id"], 0)
            await job_store.transition(job["id"], {"running"}, "awaiting_approval", {"fixture": True})
            assert (await job_store.get_job(job["id"]))["data"]["fixture"]
            other = await job_store.save_workspace("Processes", {"paths": ["b.py"]}, False, [profile.api_key_id])
            second, _ = await job_store.create_job(profile.api_key_id, profile, other, "Process fixture", ["b.py"], "process-claim")
            claims = await multiprocess_claims()
            assert claims.count(second["id"]) == 1 and claims.count(None) == 7
        try:
            async with _connect() as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute("UPDATE identities SET active=0 WHERE id=?", (identity["identity_id"],))
                raise ValueError("rollback fixture")
        except ValueError:
            pass
        assert await authenticate(identity["token"])
        await verify_shared_capacity()
        await verify_team_http(profile, identity)
        await verify_migration_restore(container, settings.database_url)
        print(json.dumps({"backend": "PostgreSQL", "image": IMAGE, "no_published_ports": True, "runtime_without_superuser_createdb_createrole": True, "repeated_schema": True, "human_identity": True, "concurrent_budget_reservations": True, "eight_processes_single_claim": True, "shared_capacity_and_quota_across_processes": True, "job_single_claim_and_replay": True, "rollback": True, "sqlite_migration_verified_by_row_hash": True, "postgres_dump_restore": True, "keys_permissions_jobs_preserved": True, "passed": True}, indent=2))
    finally:
        settings.database_url = original
        docker("rm", "-f", container)


if __name__ == "__main__":
    asyncio.run(verify())
