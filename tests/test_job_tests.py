import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_jobs as jobs
from test_jobs import KEY, start, wait_state

from app.config import settings
from app.test_sandbox import run_snapshot, validate_files

environment = jobs.environment


@pytest.fixture
def job(environment):
    record = wait_state(environment, start(environment).json()["id"])
    with sqlite3.connect(settings.database_path) as db:
        row = db.execute("SELECT config_json FROM agent_workspaces WHERE id=?", (record["workspace_id"],)).fetchone()
        config = json.loads(row[0]) | {"test_profiles": ["python-unittest", "python-compile"]}
        db.execute("UPDATE agent_workspaces SET config_json=? WHERE id=?", (json.dumps(config), record["workspace_id"]))
    return record


def create(environment, job, key="test-1", **changes):
    client, _, headers, *_ = environment
    return client.post(f"/v1/jobs/{job['id']}/tests", headers={**headers, "Idempotency-Key": key}, json={"profile": "python-unittest", "proposal_hash": job["proposal"]["hash"], **changes})


def claim(environment, headers=None):
    client, _, owner, *_ = environment
    return client.post("/v1/job-tests/claim", headers=headers or owner, json={"profiles": ["python-unittest"]})


def test_test_queue_requires_workspace_permission_and_owner(environment):
    client, _, headers, *_ = environment
    record = wait_state(environment, start(environment).json()["id"])
    assert create(environment, record).status_code == 403
    assert client.get(f"/v1/jobs/{record['id']}/tests", headers={"Authorization": f"Bearer {KEY}"}).status_code == 404
    assert client.get(f"/v1/jobs/{record['id']}/tests", headers=headers).json()["data"] == []


def test_claim_once_results_replay_and_events_hide_source(environment, job):
    client, root, headers, *_ = environment
    created = create(environment, job)
    assert created.status_code == 202
    test_id = created.json()["id"]
    assert create(environment, job).json()["id"] == test_id
    assert create(environment, job, target="applied").status_code == 409
    assert claim(environment, {"Authorization": f"Bearer {KEY}"}).status_code == 204
    leased = claim(environment)
    assert leased.status_code == 200
    assert leased.json()["files"]["a.txt"] == "new a\n"
    assert (root / "a.txt").read_text() == "old a\n"
    assert claim(environment).status_code == 204
    token = {"lease_token": leased.json()["lease_token"]}
    assert client.post(f"/v1/job-tests/{test_id}/heartbeat", headers=headers, json={"lease_token": "x" * 40}).status_code == 409
    assert client.post(f"/v1/job-tests/{test_id}/heartbeat", headers={"Authorization": f"Bearer {KEY}"}, json=token).status_code == 404
    report = {**token, "state": "completed", "exit_code": 0, "output": "all tests passed", "output_truncated": False}
    result = client.post(f"/v1/job-tests/{test_id}/result", headers=headers, json=report)
    assert result.status_code == 200 and result.json()["result"]["exit_code"] == 0
    assert client.post(f"/v1/job-tests/{test_id}/result", headers=headers, json=report).status_code == 200
    assert client.post(f"/v1/job-tests/{test_id}/result", headers=headers, json={**report, "output": "changed"}).status_code == 409
    listed = client.get(f"/v1/jobs/{job['id']}/tests", headers=headers).text
    assert "lease_token" not in listed and "new a" not in listed
    events = client.get(f"/v1/jobs/{job['id']}/events?follow=false", headers=headers).text
    assert "job.test" in events and "all tests passed" not in events


def test_atomic_claim_and_active_limit(environment, job):
    assert create(environment, job).status_code == 202
    assert create(environment, job, "test-2").status_code == 202
    assert create(environment, job, "test-3").status_code == 429
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _: claim(environment), range(2)))
    assert len({response.json()["id"] for response in claimed}) == 2
    assert claim(environment).status_code == 204


def test_parent_cancellation_cancels_queued_and_signals_running_tests(environment, job):
    client, _root, headers, *_ = environment
    queued = create(environment, job).json()
    second = create(environment, job, "running-test").json()
    lease = claim(environment).json()
    assert client.post(f"/v1/jobs/{job['id']}/cancel", headers=headers).status_code == 200
    listed = client.get(f"/v1/jobs/{job['id']}/tests", headers=headers).json()["data"]
    assert {row["state"] for row in listed} == {"running", "cancelled"}
    assert claim(environment).status_code == 204
    heartbeat = client.post(f"/v1/job-tests/{lease['id']}/heartbeat", headers=headers, json={"lease_token": lease["lease_token"]})
    assert heartbeat.json()["cancel_requested"]
    assert client.delete(f"/v1/jobs/{job['id']}", headers=headers).status_code == 409
    result = client.post(f"/v1/job-tests/{lease['id']}/result", headers=headers, json={"lease_token": lease["lease_token"], "state": "cancelled", "exit_code": None, "output": ""})
    assert result.status_code == 200, result.text
    assert client.delete(f"/v1/jobs/{job['id']}", headers=headers).status_code == 200
    assert queued["id"] != second["id"]


def test_cancel_queued_and_running_and_lease_expiration(environment, job):
    client, _, headers, *_ = environment
    test_id = create(environment, job).json()["id"]
    cancelled = client.post(f"/v1/job-tests/{test_id}/cancel", headers=headers)
    assert cancelled.json()["state"] == "cancelled" and claim(environment).status_code == 204
    running_id = create(environment, job, "running").json()["id"]
    leased = claim(environment).json()
    client.post(f"/v1/job-tests/{running_id}/cancel", headers=headers)
    assert client.post(f"/v1/job-tests/{running_id}/heartbeat", headers=headers, json={"lease_token": leased["lease_token"]}).json()["cancel_requested"] is True
    with sqlite3.connect(settings.database_path) as db:
        db.execute("UPDATE agent_job_tests SET lease_until=? WHERE id=?", (time.time() - 1, running_id))
    assert claim(environment).status_code == 204  # Never replay an uncertain execution.
    assert client.get(f"/v1/jobs/{job['id']}/tests", headers=headers).json()["data"][0]["state"] == "interrupted"
    assert client.post(f"/v1/job-tests/{running_id}/result", headers=headers, json={"lease_token": leased["lease_token"], "state": "completed", "exit_code": 0}).status_code == 409


def test_applied_revision_and_bounded_results(environment, job):
    client, root, headers, *_ = environment
    assert create(environment, job, target="applied").status_code == 409
    approved = client.post(f"/v1/jobs/{job['id']}/approvals", headers=headers, json={"decision": "approve", "proposal_hash": job["proposal"]["hash"]})
    assert approved.status_code == 200
    assert create(environment, job, target="applied").status_code == 202
    (root / "b.txt").write_text("external edit")
    assert create(environment, job, "stale", target="applied").json()["error"]["code"] == "stale_workspace"
    lease = claim(environment).json()
    bad = client.post(f"/v1/job-tests/{lease['id']}/result", headers=headers, json={"lease_token": lease["lease_token"], "state": "completed", "exit_code": 1, "output": "secret " * 20_000})
    assert bad.status_code == 400 and "secret" not in bad.text


@pytest.mark.parametrize("files", [{"../outside": "x"}, {"/etc/passwd": "x"}, {".env": "x"}, {"id_rsa": "x"}, {"a\\b": "x"}, {"a.py": "x" * 256_001}, {"a.py": "a\x00"}])
def test_sandbox_validates_before_spawning_any_process(files, monkeypatch):
    monkeypatch.setattr("app.test_sandbox.subprocess.run", lambda *_args, **_kwargs: pytest.fail("Process must not start"))
    with pytest.raises(ValueError):
        run_snapshot("python-unittest", files)
    validate_files({"test_a.py": "import unittest\n"})


@pytest.mark.asyncio
async def test_late_generation_heartbeat_cannot_extend_apply_lease(tmp_path, monkeypatch):
    from app import job_store
    from app.database import _connect, create_gateway_api, init_database

    monkeypatch.setattr(settings, "database_path", tmp_path / "lease.sqlite3")
    await init_database()
    await job_store.init_jobs()
    profile, _ = await create_gateway_api(name="Lease regression", provider="ollama", model="fixture", reasoning_effort="off")
    workspace = await job_store.save_workspace("fixture", {"paths": ["a.txt"]}, False, [profile.api_key_id])
    job, _ = await job_store.create_job(profile.api_key_id, profile, workspace, "task", ["a.txt"], "request")
    await job_store.transition(job["id"], {"queued"}, "applying")
    async with _connect() as db:
        await db.execute("UPDATE agent_jobs SET lease_until=1 WHERE id=?", (job["id"],))
        await db.commit()
    await job_store.heartbeat(job["id"])
    assert (await job_store.get_job(job["id"]))["lease_until"] == 1
