import sqlite3

import pytest

from app.config import settings
from app.database import SCHEMA_SQL, create_gateway_api, init_database


@pytest.mark.asyncio
async def test_upstream_migration_preserves_profiles_keys_and_skill_links(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    monkeypatch.setattr(settings, "database_path", path)
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_SQL.replace(", 'openai_compatible'", ""))
        connection.execute("INSERT INTO api_keys(id,name,key_hint,key_hash) VALUES (1,'existing','hint','hash')")
        connection.execute("INSERT INTO gateway_apis(id,name,slug,api_key_id,provider,model,reasoning_effort) VALUES (1,'existing','existing',1,'codex','model','auto')")
        connection.execute("INSERT INTO skills(id,name,slug,instructions) VALUES (1,'skill','skill','instructions')")
        connection.execute("INSERT INTO gateway_api_skills(gateway_api_id,skill_id,position) VALUES (1,1,0)")
    await init_database()
    await init_database()  # Repeated startup must be safe.
    profile, _ = await create_gateway_api(name="upstream", provider="openai_compatible", model="test-model", reasoning_effort="auto")
    assert profile.provider == "openai_compatible"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT slug,provider FROM gateway_apis WHERE id=1").fetchone() == ("existing", "codex")
        assert connection.execute("SELECT gateway_api_id,skill_id FROM gateway_api_skills").fetchone() == (1,1)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_gateway_apis_provider_model'").fetchone()
