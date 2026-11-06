import json
import os
import sqlite3
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.config_bundles import (
    Bundle,
    export_bundle,
    import_templates,
    read_bundle,
    write_private,
)
from app.database import SCHEMA_SQL


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "configuration.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA_SQL)
        db.execute("INSERT INTO api_keys(id,name,key_hint,key_hash,limits_json) VALUES (1,'profile','PRIVATE_HINT','PRIVATE_HASH',?)", (json.dumps({"input_tokens": 32000, "output_tokens": 4000, "token_budget": 1000000, "budget_period": "monthly"}),))
        db.execute("INSERT INTO gateway_apis(id,name,slug,api_key_id,provider,model,reasoning_effort) VALUES (1,'Portable profile','portable',1,'codex','fixture','low')")
        db.execute("INSERT INTO skills(id,name,slug,description,instructions) VALUES (1,'Portable skill','skill','Description','Preserve BLUE')")
        db.execute("INSERT INTO mcp_servers(id,name,slug,url,auth_type,auth_ciphertext) VALUES (1,'Portable MCP','mcp','http://mcp-host:8000/mcp','bearer','PRIVATE_MCP_CREDENTIAL')")
        db.execute("INSERT INTO gateway_api_skills VALUES (1,1,0)")
        db.execute("INSERT INTO gateway_api_mcp_tools VALUES (1,1,'read_file')")
        db.execute("INSERT INTO provider_connections(provider,configuration_ciphertext,tokens_ciphertext) VALUES ('gemini','PRIVATE_CONFIG','PRIVATE_PROVIDER_TOKEN')")
    return path


def test_export_roundtrip_preserves_policy_and_links_but_excludes_managed_secrets(database, tmp_path):
    bundle = export_bundle(database)
    data = bundle.model_dump()
    assert data["profiles"][0]["limits"]["input_tokens"] == 32000
    assert data["profiles"][0]["skills"] == ["Portable skill"]
    assert data["profiles"][0]["mcp_tools"] == [{"server": "Portable MCP", "tools": ["read_file"]}]
    assert data["skills"][0]["instructions"] == "Preserve BLUE"
    assert "PRIVATE_" not in json.dumps(data)
    destination = tmp_path / "bundle.json"
    write_private(destination, bundle.model_dump_json())
    assert os.stat(destination).st_mode & 0o777 == 0o600
    loaded = read_bundle(destination)
    assert loaded.fingerprint() == bundle.fingerprint()


def test_import_is_idempotent_inert_and_requires_the_exact_review_hash(database):
    bundle = export_bundle(database)
    with pytest.raises(ValueError):
        import_templates(database, bundle, "wrong")
    result = import_templates(database, bundle, bundle.fingerprint())
    assert result["state"] == "templates_only" and result["credentials_included"] is False
    assert import_templates(database, bundle, bundle.fingerprint()) == result
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM configuration_templates").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM gateway_apis").fetchone()[0] == 1
        assert db.execute("SELECT status,key_hash FROM api_keys").fetchone() == ("active", "PRIVATE_HASH")


@pytest.mark.parametrize("changes", [{"workspace_grants": ["/etc"]}, {"profiles": [{"name": "bad", "provider": "codex", "model": "x", "reasoning_effort": "low", "api_key": "PRIVATE_KEY"}]}, {"mcp_servers": [{"name": "bad", "url": "http://user:PRIVATE_KEY@host/mcp"}]}, {"profiles": [{"name": "bad", "provider": "codex", "model": "x", "reasoning_effort": "low", "skills": ["missing"]}]}])
def test_bundle_rejects_credentials_grants_and_dangling_references(changes):
    with pytest.raises(ValidationError):
        Bundle.model_validate(changes)


def test_private_export_never_overwrites_existing_files_or_symlinks(tmp_path):
    target = tmp_path / "target"
    target.write_text("preserve")
    link = tmp_path / "link"
    link.symlink_to(target)
    for path in (target, link):
        with pytest.raises(FileExistsError):
            write_private(path, "overwrite")
    assert target.read_text() == "preserve"


def test_cli_checks_and_imports_and_does_not_echo_invalid_secret(database, tmp_path):
    output = tmp_path / "bundle.json"
    exported = subprocess.run([sys.executable, "-m", "app.config_bundles", "export", "--database", str(database), "--output", str(output)], capture_output=True, text=True, check=False)
    assert exported.returncode == 0
    checked = subprocess.run([sys.executable, "-m", "app.config_bundles", "check", "--input", str(output)], capture_output=True, text=True, check=False)
    assert checked.returncode == 0
    fingerprint = json.loads(checked.stdout)["approval_hash"]
    imported = subprocess.run([sys.executable, "-m", "app.config_bundles", "import", "--database", str(database), "--input", str(output), "--approval-hash", fingerprint], capture_output=True, text=True, check=False)
    assert imported.returncode == 0 and json.loads(imported.stdout)["state"] == "templates_only"
    output.write_text('{"api_key":"PRIVATE_KEY_MUST_NOT_ECHO"}')
    rejected = subprocess.run([sys.executable, "-m", "app.config_bundles", "check", "--input", str(output)], capture_output=True, text=True, check=False)
    assert rejected.returncode != 0 and "PRIVATE_KEY" not in rejected.stdout + rejected.stderr
