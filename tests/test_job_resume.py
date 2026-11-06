import json

import test_jobs as jobs

from app import job_store
from app.config import settings
from app.database import _connect, create_api_key
from app.jobs import proposal_from_text

environment = jobs.environment


async def execute(statement, parameters=()):
    async with _connect() as db:
        result = await db.execute(statement, parameters)
        rows = await result.fetchall()
        await db.commit()
        return rows


def test_resume_is_explicit_idempotent_and_invalidates_old_approval(environment):
    client, root, headers, *_ = environment
    first = jobs.wait_state(environment, jobs.start(environment).json()["id"])
    assert client.post(f"/v1/jobs/{first['id']}/cancel", headers=headers).status_code == 200
    response = client.post(f"/v1/jobs/{first['id']}/resume", headers={**headers, "Idempotency-Key": "resume-once"}, json={})
    assert response.status_code == 202, response.text
    second = jobs.wait_state(environment, first["id"])
    assert second["revision"] == 1 and second["proposal"]["hash"] != first["proposal"]["hash"]
    assert jobs.approval(environment, second, first["proposal"]["hash"]).status_code == 409
    replay = client.post(f"/v1/jobs/{first['id']}/resume", headers={**headers, "Idempotency-Key": "resume-once"}, json={})
    assert replay.status_code == 200 and replay.json()["revision"] == 1
    changed = client.post(f"/v1/jobs/{first['id']}/resume", headers={**headers, "Idempotency-Key": "resume-once"}, json={"instruction": "Different task"})
    assert changed.status_code == 409
    assert jobs.approval(environment, second).status_code == 200
    assert (root / "a.txt").read_text() == "new a\n"
    assert client.post(f"/v1/jobs/{first['id']}/resume", headers={**headers, "Idempotency-Key": "new-resume"}, json={}).status_code == 409


def test_revision_rejection_also_invalidates_old_hash(environment):
    client, _root, headers, *_ = environment
    first = jobs.wait_state(environment, jobs.start(environment).json()["id"])
    response = client.post(f"/v1/jobs/{first['id']}/approvals", headers=headers, json={"proposal_hash": first["proposal"]["hash"], "decision": "reject", "revised_instruction": "Try again"})
    assert response.status_code == 200
    second = jobs.wait_state(environment, first["id"])
    assert second["revision"] == 1 and second["proposal"]["hash"] != first["proposal"]["hash"]
    assert jobs.approval(environment, second, first["proposal"]["hash"]).status_code == 409


def test_approval_hash_is_bound_to_base_and_revision():
    text = json.dumps({"changes": [{"path": "a.py", "content": "new"}]})
    first = proposal_from_text(text, {"a.py": {"content": "old"}}, 0)
    assert first["hash"] != proposal_from_text(text, {"a.py": {"content": "other base"}}, 0)["hash"]
    assert first["hash"] != proposal_from_text(text, {"a.py": {"content": "old"}}, 1)["hash"]


def test_resume_and_history_are_owner_scoped_and_do_not_forget_active_jobs(environment):
    client, root, headers, *_ = environment
    job = jobs.wait_state(environment, jobs.start(environment).json()["id"])
    _, key = client.portal.call(create_api_key, "Other owner")
    other = {"Authorization": "Bearer " + key}
    assert client.get("/v1/jobs", headers=other).json()["data"] == []
    assert client.post(f"/v1/jobs/{job['id']}/resume", headers={**other, "Idempotency-Key": "other"}, json={}).status_code == 404
    assert client.delete(f"/v1/jobs/{job['id']}", headers=other).status_code == 404
    listed = client.get("/v1/jobs?limit=1", headers=headers)
    assert listed.json()["data"][0]["id"] == job["id"]
    assert "new a" not in listed.text and "snapshot" not in listed.text and "worker_token" not in listed.text
    assert client.delete(f"/v1/jobs/{job['id']}", headers=headers).status_code == 409
    assert client.post(f"/v1/jobs/{job['id']}/cancel", headers=headers).status_code == 200
    assert client.delete(f"/v1/jobs/{job['id']}", headers=headers).json()["deleted"]
    assert client.get(f"/v1/jobs/{job['id']}", headers=headers).status_code == 404
    assert (root / "a.txt").read_text() == "old a\n"


def test_immutable_job_profile_revalidates_paths_and_grants(environment, monkeypatch):
    async def complete(_app, _profile, request):
        selected = json.loads(request.messages[-1].text_content())["files"]
        return json.dumps({"changes": [{"path": name, "content": "new " + name + "\n"} for name in selected]})
    monkeypatch.setattr("app.main._job_complete", complete)
    client, _root, headers, workspace, *_ = environment
    profile = client.post("/v1/job-profiles", headers=headers, json={"name": "Generic review", "workspace_id": workspace["id"], "instruction": "Review the selected files.", "paths": ["a.txt"]})
    assert profile.status_code == 201, profile.text
    profile = profile.json()
    run = client.post(f"/v1/job-profiles/{profile['id']}/jobs", headers={**headers, "Idempotency-Key": "recipe-run"}, json={"instruction": "Improve clarity."})
    assert run.status_code == 202, run.text
    job = jobs.wait_state(environment, run.json()["id"])
    assert job["paths"] == ["a.txt"] and job["job_profile"]["fingerprint"] == profile["fingerprint"]
    assert "Improve clarity." in job["instruction"]
    _, key = client.portal.call(create_api_key, "Other profile owner")
    other = {"Authorization": "Bearer " + key}
    assert client.get("/v1/job-profiles", headers=other).json()["data"] == []
    assert client.get(f"/v1/job-profiles/{profile['id']}", headers=other).status_code == 404
    client.post(f"/v1/jobs/{job['id']}/cancel", headers=headers)
    client.portal.call(execute, "DELETE FROM agent_workspace_grants WHERE workspace_id=?", (workspace["id"],))
    assert client.post(f"/v1/job-profiles/{profile['id']}/jobs", headers={**headers, "Idempotency-Key": "revoked-run"}, json={}).status_code == 404
    assert client.post(f"/v1/jobs/{job['id']}/resume", headers={**headers, "Idempotency-Key": "revoked-resume"}, json={}).status_code == 404
    assert client.delete(f"/v1/job-profiles/{profile['id']}", headers=headers).status_code == 204
    assert client.get(f"/v1/jobs/{job['id']}", headers=headers).json()["job_profile"]["id"] == profile["id"]


def test_retention_is_opt_in_and_preserves_jobs_requiring_recovery(environment, monkeypatch):
    client, _root, headers, *_ = environment
    first = jobs.wait_state(environment, jobs.start(environment).json()["id"])
    client.post(f"/v1/jobs/{first['id']}/cancel", headers=headers)
    client.portal.call(execute, "UPDATE agent_jobs SET updated_at=1 WHERE id=?", (first["id"],))
    assert client.portal.call(job_store.prune_history) == 0
    monkeypatch.setattr(settings, "jobs_retention_days", 1)
    assert client.portal.call(job_store.prune_history) == 1
    assert jobs.start(environment).status_code == 410
    second = jobs.wait_state(environment, jobs.start(environment, key="recovery-job").json()["id"])
    client.portal.call(execute, "UPDATE agent_jobs SET state='failed',updated_at=1,data_json=? WHERE id=?", (json.dumps({"error_code": "workspace_recovery_required", "instruction": "task", "paths": ["a.txt"]}), second["id"]))
    assert client.portal.call(job_store.prune_history) == 0
    assert client.delete(f"/v1/jobs/{second['id']}", headers=headers).status_code == 409
    assert client.post(f"/v1/jobs/{second['id']}/resume", headers={**headers, "Idempotency-Key": "unsafe-resume"}, json={}).status_code == 409
