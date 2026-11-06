import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import workspaces
from app.config import settings
from app.main import app

ADMIN = {"Origin": "http://127.0.0.1", "X-OmniProxy-Request": "dashboard"}
KEY = "sk-local-jobs-test-key-with-thirty-two-characters"


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.txt").write_text("old a\n")
    (root / "b.txt").write_text("old b\n")
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)
    monkeypatch.setattr(settings, "job_workspace_roots", str(root))

    async def catalog(_app):
        return [{"id": "ollama", "connected": True, "models": [{"id": "fixture", "reasoning_efforts": ["off"]}]}]

    calls = []

    async def complete(_app, profile, request):
        calls.append(request)
        return json.dumps({"changes": [{"path": "a.txt", "content": "new a\n"}, {"path": "b.txt", "content": "new b\n"}]})

    monkeypatch.setattr("app.main._model_catalog", catalog)
    monkeypatch.setattr("app.main._job_complete", complete)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        profile = client.post("/api/gateway-apis", headers=ADMIN, json={"name": "Job profile", "provider": "ollama", "model": "fixture", "reasoning_effort": "off"})
        assert profile.status_code == 201
        headers = {"Authorization": f"Bearer {profile.json()['api_key']}"}
        workspace = client.post("/api/workspaces", headers=ADMIN, json={"name": "Test repo", "root": str(root), "paths": ["a.txt", "b.txt"], "api_key_ids": [profile.json()["api_key_id"]], "writable": True})
        assert workspace.status_code == 201
        yield client, root, headers, workspace.json(), profile.json(), calls


def start(env, key="request-1", instruction="Update files"):
    client, _, headers, workspace, *_ = env
    return client.post("/v1/jobs", headers={**headers, "Idempotency-Key": key}, json={"workspace_id": workspace["id"], "instruction": instruction})


def wait_state(env, job_id, expected="awaiting_approval"):
    client, _, headers, *_ = env
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}", headers=headers)
        assert response.status_code == 200
        job = response.json()
        if job["state"] == expected:
            return job
        if job["state"] in {"failed", "cancelled", "completed"}:
            pytest.fail(f"Unexpected terminal state: {job['state']} {job.get('error_code')}")
        time.sleep(0.02)
    pytest.fail(f"Job did not reach {expected}")


def approval(env, job, proposal_hash=None):
    client, _, headers, *_ = env
    return client.post(f"/v1/jobs/{job['id']}/approvals", headers=headers, json={"decision": "approve", "proposal_hash": proposal_hash or job["proposal"]["hash"]})


def test_job_proposal_approval_idempotency_and_event_replay(environment):
    client, root, headers, _workspace, _, calls = environment
    first = start(environment)
    assert first.status_code == 202
    job = wait_state(environment, first.json()["id"])
    assert (root / "a.txt").read_text() == "old a\n"
    assert "root" not in client.get("/v1/workspaces", headers=headers).text
    repeated = start(environment)
    assert repeated.status_code == 200 and repeated.json()["id"] == job["id"]
    assert len(calls) == 1
    assert start(environment, instruction="different").status_code == 409
    assert approval(environment, job, "0" * 64).status_code == 409
    approved = approval(environment, job)
    assert approved.status_code == 200 and approved.json()["state"] == "completed"
    assert (root / "a.txt").read_text() == "new a\n"
    assert (root / "b.txt").read_text() == "new b\n"
    modified = (root / "a.txt").stat().st_mtime_ns
    assert approval(environment, job).json()["state"] == "completed"
    assert (root / "a.txt").stat().st_mtime_ns == modified
    events = client.get(f"/v1/jobs/{job['id']}/events", headers=headers)
    ids = [int(line[4:]) for line in events.text.splitlines() if line.startswith("id: ")]
    assert ids == sorted(set(ids)) and len(ids) >= 5
    resumed = client.get(f"/v1/jobs/{job['id']}/events", headers={**headers, "Last-Event-ID": str(ids[-2])})
    assert [int(line[4:]) for line in resumed.text.splitlines() if line.startswith("id: ")] == ids[-1:]


def test_stale_patch_does_not_write_any_file(environment):
    _, root, *_ = environment
    job = wait_state(environment, start(environment).json()["id"])
    (root / "b.txt").write_text("external change\n")
    response = approval(environment, job)
    assert response.status_code == 409 and response.json()["error"]["code"] == "stale_workspace"
    assert (root / "a.txt").read_text() == "old a\n"
    assert (root / "b.txt").read_text() == "external change\n"


def test_cancel_running_job_stops_generation(environment, monkeypatch):
    import asyncio
    client, root, headers, *_ = environment
    started, cancelled = threading.Event(), threading.Event()

    async def complete(*_):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    monkeypatch.setattr("app.main._job_complete", complete)
    job_id = start(environment).json()["id"]
    assert started.wait(3)
    response = client.post(f"/v1/jobs/{job_id}/cancel", headers=headers)
    assert response.status_code == 200 and response.json()["state"] == "cancelled"
    assert cancelled.wait(3)
    assert client.post(f"/v1/jobs/{job_id}/cancel", headers=headers).json()["state"] == "cancelled"
    assert (root / "a.txt").read_text() == "old a\n"


def test_job_access_is_scoped_to_key(environment):
    client, _, _, _workspace, *_ = environment
    job = wait_state(environment, start(environment).json()["id"])
    other = {"Authorization": f"Bearer {KEY}"}
    assert client.get(f"/v1/jobs/{job['id']}", headers=other).status_code == 404
    assert client.get(f"/v1/jobs/{job['id']}/events", headers=other).status_code == 404
    assert client.post(f"/v1/jobs/{job['id']}/cancel", headers=other).status_code == 404
    assert client.post(f"/v1/jobs/{job['id']}/approvals", headers=other, json={"decision": "approve", "proposal_hash": job["proposal"]["hash"]}).status_code == 404
    assert client.get("/v1/workspaces", headers=other).json()["data"] == []


def test_workspace_rejects_traversal_secrets_symlinks_and_hardlinks(environment):
    client, root, _, _, profile, *_ = environment
    outside = root.parent / "outside.txt"
    outside.write_text("private")
    (root / "link.txt").symlink_to(outside)
    os.link(outside, root / "hard.txt")
    for path in ("../outside.txt", ".env", "link.txt", "hard.txt", "/etc/passwd"):
        response = client.post("/api/workspaces", headers=ADMIN, json={"name": "unsafe", "root": str(root), "paths": [path], "api_key_ids": [profile["api_key_id"]]})
        assert response.status_code == 400
        assert "private" not in response.text


def test_invalid_model_proposal_is_not_applied(environment, monkeypatch):
    async def complete(*_):
        return '{"changes":[{"path":"../outside.txt","content":"unsafe"}]}'
    monkeypatch.setattr("app.main._job_complete", complete)
    job = wait_state(environment, start(environment).json()["id"], "failed")
    assert job["error_code"] == "invalid_job_proposal"


def test_interrupted_partial_apply_is_rolled_back_from_journal(environment):
    _, root, *_ = environment
    job = wait_state(environment, start(environment).json()["id"])
    (root / "a.txt").write_text("new a\n")
    with sqlite3.connect(settings.database_path) as db:
        db.execute("UPDATE agent_jobs SET state='applying',lease_until=? WHERE id=?", (time.time() - 1, job["id"]))
    recovered = wait_state(environment, job["id"], "failed")
    assert recovered["error_code"] == "interrupted_apply_rolled_back"
    assert (root / "a.txt").read_text() == "old a\n"
    assert (root / "b.txt").read_text() == "old b\n"


def test_write_failure_rolls_back_completed_files(environment, monkeypatch):
    _, root, *_ = environment
    job = wait_state(environment, start(environment).json()["id"])
    original = workspaces._write

    def fail_second(root_fd, path, content, expected_hash, mode):
        if path == "b.txt" and content == "new b\n":
            raise OSError("simulated disk failure")
        return original(root_fd, path, content, expected_hash, mode)

    monkeypatch.setattr(workspaces, "_write", fail_second)
    response = approval(environment, job)
    assert response.status_code == 500
    assert (root / "a.txt").read_text() == "old a\n"
    assert (root / "b.txt").read_text() == "old b\n"


def test_concurrent_duplicate_requests_create_one_job(environment):
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: start(environment), range(2)))
    assert {response.status_code for response in responses} == {200, 202}
    assert len({response.json()["id"] for response in responses}) == 1


def test_read_only_workspace_and_swapped_symlink_cannot_be_written(environment):
    _, root, _, workspace, *_ = environment
    job = wait_state(environment, start(environment).json()["id"])
    with sqlite3.connect(settings.database_path) as db:
        db.execute("UPDATE agent_workspaces SET writable=0 WHERE id=?", (workspace["id"],))
    assert approval(environment, job).status_code == 403
    with sqlite3.connect(settings.database_path) as db:
        db.execute("UPDATE agent_workspaces SET writable=1 WHERE id=?", (workspace["id"],))
    outside = root.parent / "external.txt"
    outside.write_text("external")
    (root / "b.txt").unlink()
    (root / "b.txt").symlink_to(outside)
    assert approval(environment, job).status_code == 400
    assert (root / "a.txt").read_text() == "old a\n"
    assert outside.read_text() == "external"


def test_nested_mount_and_root_replacement_are_rejected(environment, monkeypatch):
    client, root, _, _workspace, profile, *_ = environment
    (root / "nested").mkdir()
    (root / "nested" / "file.txt").write_text("data")
    monkeypatch.setattr(workspaces, "_mountpoints", lambda: [root / "nested"])
    response = client.post("/api/workspaces", headers=ADMIN, json={"name": "mount", "root": str(root), "paths": ["nested/file.txt"], "api_key_ids": [profile["api_key_id"]]})
    assert response.status_code == 400


def test_worker_shutdown_requeues_only_read_only_generation(environment, monkeypatch):
    import asyncio
    client, root, *_ = environment
    started = threading.Event()

    async def slow(*_):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr("app.main._job_complete", slow)
    job_id = start(environment).json()["id"]
    assert started.wait(3)
    client.portal.call(app.state.job_runner.stop)
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute("SELECT state FROM agent_jobs WHERE id=?", (job_id,)).fetchone()[0] == "queued"

    async def fast(*_):
        return '{"changes":[{"path":"a.txt","content":"resumed\\n"}]}'

    monkeypatch.setattr("app.main._job_complete", fast)
    client.portal.call(app.state.job_runner.start)
    wait_state(environment, job_id)
    assert (root / "a.txt").read_text() == "old a\n"


def test_rejected_proposal_can_be_revised(environment):
    client, root, headers, *_ = environment
    job = wait_state(environment, start(environment).json()["id"])
    response = client.post(f"/v1/jobs/{job['id']}/approvals", headers=headers, json={"decision": "reject", "proposal_hash": job["proposal"]["hash"], "revised_instruction": "Revise the proposed change"})
    assert response.status_code == 200 and response.json()["state"] == "queued"
    revised = wait_state(environment, job["id"])
    assert revised["instruction"] == "Revise the proposed change"
    assert (root / "a.txt").read_text() == "old a\n"


@pytest.mark.asyncio
async def test_codex_job_explicitly_disables_client_and_server_tools(monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI, Request

    from app.main import _complete_without_tools
    from app.provider_broker import CodexBrokerClient
    from app.routing import ModelRoute
    from app.schemas import ChatCompletionRequest

    async def complete(self, request, *, model, reasoning_effort):
        assert request.tools == [] and request.tool_choice == "none"
        assert request.parallel_tool_calls is False
        return {"content": '{"changes":[]}', "tool_calls": []}

    monkeypatch.setattr(CodexBrokerClient, "complete_with_tools", complete)
    instance = FastAPI()
    instance.state.codex_broker_http = object()
    profile = SimpleNamespace(slug="job", provider="codex", model="fixture", reasoning_effort="low")
    request = Request({"type": "http", "app": instance, "headers": []})
    result = await _complete_without_tools(request, ModelRoute(profile.slug, profile.provider, profile.model, profile.reasoning_effort), ChatCompletionRequest.model_validate({"model": "job", "messages": [{"role": "user", "content": "Analyze only"}]}))
    assert result == '{"changes":[]}'
