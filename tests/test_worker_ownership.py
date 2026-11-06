import pytest

from app import job_store
from app.config import settings
from app.database import _connect, create_gateway_api, init_database
from app.jobs import JobRunner


@pytest.mark.asyncio
async def test_stale_worker_cannot_heartbeat_publish_or_requeue_new_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "ownership.sqlite3")
    await init_database()
    await job_store.init_jobs()
    profile, _ = await create_gateway_api(name="Ownership", provider="ollama", model="fixture", reasoning_effort="off")
    workspace = await job_store.save_workspace("Fixture", {"paths": ["a.py"]}, False, [profile.api_key_id])
    job, _ = await job_store.create_job(profile.api_key_id, profile, workspace, "Fixture", ["a.py"], "request")
    old = await job_store.claim()
    async with _connect() as db:
        await db.execute("UPDATE agent_jobs SET lease_until=1 WHERE id=?", (job["id"],))
        await db.commit()
    assert not await job_store.heartbeat(job["id"], old["worker_token"])
    await JobRunner(None).recover()
    new = await job_store.claim()
    assert new["worker_token"] != old["worker_token"]
    assert not await job_store.heartbeat(job["id"], old["worker_token"])
    assert await job_store.heartbeat(job["id"], new["worker_token"])
    assert await job_store.transition(job["id"], {"running"}, "awaiting_approval", {"proposal": "stale"}, worker_token=old["worker_token"]) is None
    assert await job_store.transition(job["id"], {"running"}, "queued", worker_token=old["worker_token"], expired_only=True) is None
    result = await job_store.transition(job["id"], {"running"}, "awaiting_approval", {"proposal": "current"}, worker_token=new["worker_token"])
    assert result["data"]["proposal"] == "current"


@pytest.mark.asyncio
async def test_newer_database_schema_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "newer.sqlite3")
    await init_database()
    async with _connect() as db:
        await db.execute("UPDATE schema_versions SET version=999 WHERE component='core'")
        await db.commit()
    with pytest.raises(RuntimeError, match="newer"):
        await init_database()
