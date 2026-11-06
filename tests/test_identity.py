import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from functools import partial

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import _connect, init_database
from app.identity import extend_operator_session, provision, session_anniversary
from app.main import app
from app.providers.ollama import OllamaProvider

LEGACY = "sk-local-identity-legacy-key-with-thirty-two-characters"


def auth(token):
    return {"Authorization": "Bearer " + token}


def test_five_year_operator_extension_keeps_token_and_revocation_effective(team):
    client, operator, _tenant, _project, _profile = team
    old = client.get("/admin/v1/me", headers=auth(operator["token"])).json()
    extended = client.portal.call(partial(extend_operator_session, operator["session_id"], operator["token"], years=5))
    current = client.get("/admin/v1/me", headers=auth(operator["token"])).json()
    assert current["session_id"] == old["session_id"] == extended["session_id"]
    expires = datetime.fromisoformat(extended["expires_at"])
    assert expires.year == datetime.now(timezone.utc).year + 5
    assert current["expires_at"] > old["expires_at"] + 4 * 365 * 86400
    assert extended["credential_preserved"] is True and "token" not in extended
    cookie = client.post("https://127.0.0.1/admin/v1/session", headers=auth(operator["token"]))
    assert cookie.status_code == 200 and "Secure" in cookie.headers["set-cookie"]
    assert client.delete("/admin/v1/sessions/" + operator["session_id"], headers=auth(operator["token"])).status_code == 204
    assert client.get("/admin/v1/me", headers=auth(operator["token"])).status_code == 401
    with pytest.raises(ValueError, match="active operator"):
        client.portal.call(partial(extend_operator_session, operator["session_id"], operator["token"], years=5))


@pytest.mark.parametrize("condition", ["wrong_token", "revoked", "inactive", "expired", "not_operator"])
def test_long_extension_cannot_bypass_identity_session_boundaries(team, condition):
    client, operator, _tenant, _project, _profile = team
    changes = {"revoked": ("UPDATE identity_sessions SET revoked=1 WHERE id=?", operator["session_id"]), "inactive": ("UPDATE identities SET active=0 WHERE id=?", operator["identity_id"]), "expired": ("UPDATE identity_sessions SET expires_at=1 WHERE id=?", operator["session_id"]), "not_operator": ("UPDATE identities SET is_operator=0 WHERE id=?", operator["identity_id"])}
    if condition in changes:
        statement, identifier = changes[condition]
        client.portal.call(sql, statement, (identifier,))
    before = client.portal.call(sql, "SELECT expires_at,token_hash FROM identity_sessions WHERE id=?", (operator["session_id"],))
    token = "wrong-fixture-token" if condition == "wrong_token" else operator["token"]
    with pytest.raises(ValueError, match="active operator"):
        client.portal.call(partial(extend_operator_session, operator["session_id"], token, years=5))
    after = client.portal.call(sql, "SELECT expires_at,token_hash FROM identity_sessions WHERE id=?", (operator["session_id"],))
    assert tuple(before[0]) == tuple(after[0])


def test_explicit_session_years_handle_leap_day_and_reject_invalid_durations():
    leap = datetime(2028, 2, 29, 12, tzinfo=timezone.utc)
    assert session_anniversary(leap, 5) == datetime(2033, 2, 28, 12, tzinfo=timezone.utc)
    for years in (0, 6, -1, True, 1.5):
        with pytest.raises(ValueError):
            session_anniversary(leap, years)


def test_operator_extension_cli_exports_metadata_without_secret(team, tmp_path):
    client, operator, _tenant, _project, _profile = team
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(operator))
    path.chmod(0o600)
    result = subprocess.run([sys.executable, "-m", "scripts.extend_operator_session", "--database", str(settings.database_path), "--credential-file", str(path), "--years", "5"], check=True, capture_output=True, text=True)
    assert operator["token"] not in result.stdout + result.stderr
    assert json.loads(result.stdout)["credential_preserved"] is True
    assert json.loads(path.read_text()) == operator
    assert client.get("/admin/v1/me", headers=auth(operator["token"])).status_code == 200


async def sql(statement, params=()):
    async with _connect() as db:
        cursor = await db.execute(statement, params)
        result = await cursor.fetchall()
        await db.commit()
        return result


@pytest.fixture
def team(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "team.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", LEGACY)
    monkeypatch.setattr(settings, "admin_auth_required", True)

    async def valid(*_):
        return None

    async def complete(*_):
        return "BLUE"

    async def client(*_):
        return object()

    monkeypatch.setattr("app.main._validate_gateway_api_config", valid)
    monkeypatch.setattr("app.main._require_ollama_client", client)
    monkeypatch.setattr(OllamaProvider, "complete", complete)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        operator = client.portal.call(partial(provision, "Operator", operator=True))
        token = auth(operator["token"])
        tenant = client.post("/admin/v1/tenants", headers=token, json={"name": "Team One"}).json()["id"]
        project = client.post(f"/admin/v1/tenants/{tenant}/projects", headers=token, json={"name": "Project One"}).json()["id"]
        profile = client.post(f"/admin/v1/projects/{project}/profiles", headers=token, json={"name": "Local model", "provider": "ollama", "model": "fixture", "reasoning_effort": "off"})
        assert profile.status_code == 201, profile.text
        yield client, operator, tenant, project, profile.json()


def person(client, tenant, role):
    return client.portal.call(partial(provision, role.title(), tenant=tenant, role=role))


def key(client, token, project, profile, limits=None):
    response = client.post(f"/admin/v1/projects/{project}/keys", headers=auth(token), json={"name": "Private application", "profile_id": profile["id"], "limits": limits or {}})
    assert response.status_code == 201, response.text
    return response.json()


def test_admin_and_application_credentials_are_distinct_and_personal_mode_survives(team, monkeypatch):
    client, operator, _tenant, project, profile = team
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/gateway-apis").status_code == 401
    assert client.get("/admin/v1/me", headers=auth(LEGACY)).status_code == 401
    assert client.get("/v1/models", headers=auth(operator["token"])).status_code == 401
    assert client.get("/v1/models", headers=auth(LEGACY)).status_code == 401
    minted = key(client, operator["token"], project, profile)
    assert client.get("/admin/v1/me", headers=auth(minted["api_key"])).status_code == 401
    assert client.get("/v1/models", headers=auth(minted["api_key"])).json()["data"][0]["id"] == profile["slug"]
    monkeypatch.setattr(settings, "admin_auth_required", False)
    assert client.get("/v1/models", headers=auth(LEGACY)).status_code == 200
    # New administration never becomes public in personal mode.
    assert client.get("/admin/v1/me").status_code == 401


def test_reusable_profile_has_independent_keys_limits_usage_and_revocation(team):
    client, _operator, tenant, project, profile = team
    manager = person(client, tenant, "admin")
    first = key(client, manager["token"], project, profile, {"output_tokens": 1})
    second = key(client, manager["token"], project, profile, {"output_tokens": 16})
    for minted in (first, second):
        response = client.post("/v1/chat/completions", headers=auth(minted["api_key"]), json={"model": profile["slug"], "messages": [{"role": "user", "content": "Say BLUE"}]})
        assert response.status_code == 200, response.text
        assert response.json()["usage"]["completion_tokens"] <= minted["limits"]["output_tokens"]
    assert client.get("/v1/usage", headers=auth(second["api_key"])).json()["limits"]["output_tokens"] == 16
    endpoint = f"/admin/v1/projects/{project}/keys/{first['id']}"
    assert client.patch(endpoint + "/limits", headers=auth(manager["token"]), json={}).status_code == 200
    assert client.get("/v1/usage", headers=auth(first["api_key"])).json()["limits"]["output_tokens"] is None
    assert client.delete(endpoint, headers=auth(manager["token"])).status_code == 204
    assert client.get("/v1/models", headers=auth(first["api_key"])).status_code == 401
    assert client.get("/v1/models", headers=auth(second["api_key"])).status_code == 200
    rows = client.get(f"/admin/v1/projects/{project}/keys", headers=auth(manager["token"]))
    assert first["api_key"] not in rows.text and second["api_key"] not in rows.text


def test_roles_cross_tenant_and_global_tools_are_denied(team):
    client, operator, tenant, project, profile = team
    viewer = person(client, tenant, "viewer")
    manager = person(client, tenant, "admin")
    other = client.post("/admin/v1/tenants", headers=auth(operator["token"]), json={"name": "Other Team"}).json()["id"]
    assert client.get(f"/admin/v1/tenants/{other}/projects", headers=auth(manager["token"])).status_code == 403
    assert client.get("/api/mcp/servers", headers=auth(manager["token"])).status_code == 403
    assert client.post(f"/admin/v1/projects/{project}/profiles", headers=auth(manager["token"]), json={"name": "Forbidden", "provider": "codex", "model": "secret", "reasoning_effort": "auto", "skill_ids": [1]}).status_code == 403
    assert client.post(f"/admin/v1/projects/{project}/keys", headers=auth(viewer["token"]), json={"name": "Unauthorized", "profile_id": profile["id"]}).status_code == 403
    assert client.get(f"/admin/v1/projects/{project}/profiles", headers=auth(viewer["token"])).status_code == 200
    assert client.put(f"/admin/v1/tenants/{tenant}/members", headers=auth(manager["token"]), json={"identity_id": viewer["identity_id"], "role": "owner"}).status_code == 403


def test_membership_revocation_and_last_owner_protection(team):
    client, operator, tenant, project, _profile = team
    manager = person(client, tenant, "admin")
    removed = client.delete(f"/admin/v1/tenants/{tenant}/members/{manager['identity_id']}", headers=auth(operator["token"]))
    assert removed.status_code == 204
    assert client.get(f"/admin/v1/projects/{project}/profiles", headers=auth(manager["token"])).status_code == 403
    last = client.delete(f"/admin/v1/tenants/{tenant}/members/{operator['identity_id']}", headers=auth(operator["token"]))
    assert last.status_code == 409 and last.json()["error"]["code"] == "last_tenant_owner"


def test_cookie_session_csrf_expiry_and_revoke(team):
    client, operator, tenant, _project, _profile = team
    assert client.get("/", follow_redirects=False).status_code == 303
    response = client.post("/admin/v1/session", headers=auth(operator["token"]))
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
    assert client.get("/admin/v1/me").status_code == 200
    denied = client.post(f"/admin/v1/tenants/{tenant}/projects", json={"name": "CSRF project"})
    assert denied.status_code == 403
    deleted = client.delete(f"/admin/v1/sessions/{operator['session_id']}", headers={"Origin": "http://127.0.0.1", "X-OmniProxy-Request": "dashboard"})
    assert deleted.status_code == 204
    assert client.get("/admin/v1/me", headers=auth(operator["token"])).status_code == 401
    temporary = person(client, tenant, "viewer")
    client.portal.call(sql, "UPDATE identity_sessions SET expires_at=0 WHERE id=?", (temporary["session_id"],))
    assert client.get("/admin/v1/me", headers=auth(temporary["token"])).status_code == 401


def test_scope_suspension_migration_and_metadata_only_audit(team):
    client, operator, tenant, project, profile = team
    minted = key(client, operator["token"], project, profile)
    client.portal.call(init_database)
    client.portal.call(init_database)
    assert client.get("/v1/models", headers=auth(minted["api_key"])).status_code == 200
    client.portal.call(sql, "UPDATE team_projects SET active=0 WHERE id=?", (project,))
    assert client.get("/v1/models", headers=auth(minted["api_key"])).status_code == 401
    report = client.get(f"/admin/v1/tenants/{tenant}/audit", headers=auth(operator["token"]))
    assert report.status_code == 200 and report.json()["data"]
    assert minted["api_key"] not in report.text and "Private application" not in report.text and "fixture" not in report.text
    with sqlite3.connect(settings.database_path) as db:
        dumps = "\n".join(db.iterdump())
        assert minted["api_key"] not in dumps and operator["token"] not in dumps


def test_suspension_diagnostics_and_team_downgrade_are_guarded(team, monkeypatch):
    client, operator, tenant, project, profile = team
    manager = person(client, tenant, "admin")
    minted = key(client, manager["token"], project, profile)
    assert client.get(f"/admin/v1/projects/{project}/diagnostics", headers=auth(manager["token"])).status_code == 200
    assert client.get("/admin/v1/diagnostics", headers=auth(manager["token"])).status_code == 403
    assert client.get("/admin/v1/diagnostics", headers=auth(operator["token"])).status_code == 200
    endpoint = f"/admin/v1/projects/{project}/profiles/{profile['id']}"
    assert client.patch(endpoint, headers=auth(manager["token"]), json={"active": False}).status_code == 403
    assert client.patch(endpoint, headers=auth(operator["token"]), json={"active": False}).status_code == 200
    assert client.get("/v1/models", headers=auth(minted["api_key"])).status_code == 401
    assert client.patch(endpoint, headers=auth(operator["token"]), json={"active": True}).status_code == 200
    assert client.get("/v1/models", headers=auth(minted["api_key"])).status_code == 200
    endpoint = f"/admin/v1/tenants/{tenant}/projects/{project}"
    assert client.patch(endpoint, headers=auth(manager["token"]), json={"active": False}).status_code == 200
    assert client.get("/v1/models", headers=auth(minted["api_key"])).status_code == 401
    assert client.patch(endpoint, headers=auth(manager["token"]), json={"active": True}).status_code == 200
    monkeypatch.setattr(settings, "admin_auth_required", False)
    with pytest.raises(RuntimeError, match="ADMIN_AUTH_REQUIRED"), TestClient(app, base_url="http://127.0.0.1"):
        pass


def test_operator_cli_writes_only_private_new_file(tmp_path):
    output = tmp_path / "credential.json"
    command = [sys.executable, "-m", "scripts.provision_identity", "--database", str(tmp_path / "cli.sqlite3"), "--name", "Fixture operator", "--operator", "--output", str(output)]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    value = json.loads(output.read_text())
    assert value["token"].startswith("omni-admin-") and value["token"] not in result.stdout + result.stderr
    assert output.stat().st_mode & 0o777 == 0o600
    again = subprocess.run(command, check=False, capture_output=True, text=True)
    assert again.returncode != 0 and json.loads(output.read_text()) == value
    renewed = tmp_path / "renewed.json"
    result = subprocess.run([sys.executable, "-m", "scripts.provision_identity", "--database", str(tmp_path / "cli.sqlite3"), "--identity-id", value["identity_id"], "--output", str(renewed)], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    renewal = json.loads(renewed.read_text())
    assert renewal["identity_id"] == value["identity_id"] and renewal["token"] != value["token"]
    assert renewal["token"] not in result.stdout + result.stderr
