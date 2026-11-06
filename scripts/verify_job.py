"""Exercise a real, approved repository job using an isolated test workspace."""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

import httpx

from app.config import settings
from app.database import create_gateway_api
from app.main import app


async def verify(broker_url: str, model: str):
    with tempfile.TemporaryDirectory(prefix="omniproxy-job-e2e-") as temporary:
        root = Path(temporary) / "repo"
        root.mkdir()
        target = root / "hello.txt"
        target.write_text("original\n")
        settings.database_url = ""
        settings.admin_auth_required = False
        settings.dashboard_auth_required = False
        settings.require_https = False
        settings.otel_enabled = False
        settings.database_path = Path(temporary) / "gateway.sqlite3"
        settings.bootstrap_api_key = "sk-local-job-e2e-key-with-thirty-two-characters"
        settings.codex_broker_url = broker_url
        settings.job_workspace_roots = str(root)
        async with app.router.lifespan_context(app):
            profile, key = await create_gateway_api(name="Job E2E", provider="codex", model=model, reasoning_effort="low")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", timeout=180) as client:
                admin = {"Origin": "http://127.0.0.1", "X-OmniProxy-Request": "dashboard"}
                response = await client.post("/api/workspaces", headers=admin, json={"name": "E2E", "root": str(root), "paths": ["hello.txt"], "api_key_ids": [profile.api_key_id], "writable": True})
                assert response.status_code == 201
                headers = {"Authorization": f"Bearer {key}", "Idempotency-Key": "real-job-1"}
                body = {"workspace_id": response.json()["id"], "instruction": "Change hello.txt to contain exactly 'updated by job' followed by one newline."}
                response = await client.post("/v1/jobs", headers=headers, json=body)
                assert response.status_code == 202
                job_id = response.json()["id"]
                async with asyncio.timeout(150):
                    while True:
                        job = (await client.get(f"/v1/jobs/{job_id}", headers=headers)).json()
                        if job["state"] == "awaiting_approval":
                            break
                        assert job["state"] not in {"failed", "cancelled"}, job.get("error_code")
                        await asyncio.sleep(0.2)
                assert target.read_text() == "original\n"
                proposal = job["proposal"]
                assert proposal["changes"][0]["content"] == "updated by job\n"
                repeated = await client.post("/v1/jobs", headers=headers, json=body)
                assert repeated.json()["id"] == job_id
                approved = await client.post(f"/v1/jobs/{job_id}/approvals", headers=headers, json={"decision": "approve", "proposal_hash": proposal["hash"]})
                assert approved.status_code == 200 and approved.json()["state"] == "completed"
                assert target.read_text() == "updated by job\n"
                events = await client.get(f"/v1/jobs/{job_id}/events", headers=headers)
                assert events.status_code == 200 and '"state": "completed"' in events.text
                print(json.dumps({"scope": "source_asgi_temporary_workspace", "model": model, "proposal": True, "approval": True, "idempotency": True, "events": True, "passed": True}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker-url", required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    args = parser.parse_args()
    asyncio.run(verify(args.broker_url, args.model))
