"""Real Codex job recipe, persistent approval, restart and explicit resumption."""

import argparse
import asyncio
import json
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from app.config import settings
from app.database import create_gateway_api
from app.main import app


@asynccontextmanager
async def client():
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", timeout=180) as connection:
        yield connection


async def wait(connection, job_id, headers):
    async with asyncio.timeout(150):
        while True:
            result = (await connection.get(f"/v1/jobs/{job_id}", headers=headers)).json()
            if result["state"] == "awaiting_approval":
                return result
            assert result["state"] not in {"failed", "cancelled"}, result.get("error_code")
            await asyncio.sleep(0.2)


async def verify(broker_url, model):
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="omniproxy-resume-e2e-") as temporary:
        root = Path(temporary) / "repo"
        root.mkdir()
        target = root / "hello.txt"
        target.write_text("original\n")
        settings.database_path = Path(temporary) / "gateway.sqlite3"
        settings.database_url = ""
        settings.admin_auth_required = False
        settings.dashboard_auth_required = False
        settings.require_https = False
        settings.otel_enabled = False
        settings.bootstrap_api_key = "sk-local-resume-e2e-key-with-thirty-two-characters"
        settings.codex_broker_url, settings.job_workspace_roots = broker_url, str(root)
        async with client() as connection:
            profile, key = await create_gateway_api(name="Resumable job fixture", provider="codex", model=model, reasoning_effort="low")
            headers = {"Authorization": "Bearer " + key, "Idempotency-Key": "recipe-job"}
            admin = {"Origin": "http://127.0.0.1", "X-OmniProxy-Request": "dashboard"}
            workspace = await connection.post("/api/workspaces", headers=admin, json={"name": "Resume fixture", "root": str(root), "paths": ["hello.txt"], "api_key_ids": [profile.api_key_id], "writable": True})
            assert workspace.status_code == 201
            recipe = await connection.post("/v1/job-profiles", headers=headers, json={"name": "Generic text edit", "workspace_id": workspace.json()["id"], "instruction": "Change hello.txt to contain exactly 'updated by job' followed by one newline."})
            assert recipe.status_code == 201, recipe.text
            recipe = recipe.json()
            created = await connection.post(f"/v1/job-profiles/{recipe['id']}/jobs", headers=headers, json={})
            assert created.status_code == 202, created.text
            job_id = created.json()["id"]
            first = await wait(connection, job_id, headers)
            assert target.read_text() == "original\n"
        async with client() as connection:
            persisted = (await connection.get(f"/v1/jobs/{job_id}", headers=headers)).json()
            assert persisted["proposal"]["hash"] == first["proposal"]["hash"] and persisted["state"] == "awaiting_approval"
            await app.state.job_runner.stop()
            assert (await connection.post(f"/v1/jobs/{job_id}/cancel", headers=headers)).status_code == 200
            resumed = await connection.post(f"/v1/jobs/{job_id}/resume", headers={**headers, "Idempotency-Key": "resume-job"}, json={})
            assert resumed.status_code == 202 and resumed.json()["state"] == "queued"
        async with client() as connection:
            second = await wait(connection, job_id, headers)
            assert second["revision"] == 1 and second["proposal"]["hash"] != first["proposal"]["hash"]
            assert target.read_text() == "original\n"
            old = await connection.post(f"/v1/jobs/{job_id}/approvals", headers=headers, json={"decision": "approve", "proposal_hash": first["proposal"]["hash"]})
            assert old.status_code == 409
            body = {"decision": "approve", "proposal_hash": second["proposal"]["hash"]}
            assert (await connection.post(f"/v1/jobs/{job_id}/approvals", headers=headers, json=body)).json()["state"] == "completed"
            assert target.read_text() == "updated by job\n"
            stamp = (target.stat().st_ino, target.stat().st_mtime_ns)
            assert (await connection.post(f"/v1/jobs/{job_id}/approvals", headers=headers, json=body)).status_code == 200
            assert stamp == (target.stat().st_ino, target.stat().st_mtime_ns)
            events = await connection.get(f"/v1/jobs/{job_id}/events?follow=false", headers=headers)
            assert "user_resumed" in events.text and '"state": "completed"' in events.text
            assert (await connection.delete(f"/v1/jobs/{job_id}", headers=headers)).json()["deleted"]
            assert (await connection.post(f"/v1/job-profiles/{recipe['id']}/jobs", headers=headers, json={})).status_code == 410
        print(json.dumps({"scope": "three_source_gateway_lifecycles_temporary_workspace", "model": model, "recipe": True, "persistent_approval": True, "restart_and_explicit_resume": True, "old_approval_refused": True, "single_apply": True, "events": True, "history_deletion_keeps_idempotency_tombstone": True, "passed": True}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker-url", required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    args = parser.parse_args()
    asyncio.run(verify(args.broker_url, args.model))
