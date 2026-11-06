from functools import partial

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import (
    _connect,
    authenticate_api_key,
    create_gateway_api,
    get_gateway_api,
    init_database,
)
from app.identity import provision
from app.main import app

ADMIN = {"Origin": "http://127.0.0.1", "X-OmniProxy-Request": "dashboard"}
BASE = "/api/gateway-apis"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "database_path", tmp_path / "groups.sqlite3")
    monkeypatch.setattr(settings, "provider_vault_key_path", tmp_path / "vault.key")
    monkeypatch.setattr(settings, "bootstrap_api_key", "")
    monkeypatch.setattr(settings, "admin_auth_required", False)
    monkeypatch.setattr(settings, "otel_enabled", False)
    with TestClient(app, base_url="http://127.0.0.1") as session:
        yield session


def profile(client, name):
    return client.portal.call(partial(create_gateway_api, name=name, provider="ollama", model="fixture", reasoning_effort="off", limits={"input_tokens": 100, "output_tokens": 20}))


def group(client, name):
    response = client.post(BASE + "/groups", headers=ADMIN, json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


async def credential_snapshot(key_id):
    async with _connect() as db:
        row = await (await db.execute("SELECT id,key_hash,is_active,status,limits_json FROM api_keys WHERE id=?", (key_id,))).fetchone()
    return dict(row)


def test_groups_persist_and_never_change_credentials_routing_or_limits(client):
    maps, maps_key = profile(client, "Mappe")
    wiki, wiki_key = profile(client, "WikiNick")
    before = client.portal.call(credential_snapshot, maps.api_key_id)
    study = group(client, "  Studio  ")
    for item in (maps, wiki):
        moved = client.put(f"{BASE}/{item.id}/group", headers=ADMIN, json={"group_id": study["id"]})
        assert moved.status_code == 200
    collapsed = client.patch(f"{BASE}/groups/{study['id']}", headers=ADMIN, json={"collapsed": True})
    assert collapsed.status_code == 200
    client.portal.call(init_database)  # Repeat the migration as after a restart.
    persisted = client.get(BASE + "/groups").json()["data"]
    assert persisted == [{**study, "collapsed": True, "api_ids": [maps.id, wiki.id]}]
    assert client.portal.call(get_gateway_api, maps.id) == maps
    assert client.portal.call(credential_snapshot, maps.api_key_id) == before
    assert client.portal.call(authenticate_api_key, maps_key).id == maps.api_key_id
    assert client.portal.call(authenticate_api_key, wiki_key).id == wiki.api_key_id
    assert client.get(BASE + "/groups").headers["Cache-Control"] == "no-store"
    assert 'id="api-group-form"' in client.get("/").text


def test_move_rename_remove_and_delete_group_keep_keys_valid(client):
    robot, key = profile(client, "QNick robot")
    study, robots = group(client, "Studio"), group(client, "Robot")
    for destination in (study["id"], robots["id"], None, robots["id"]):
        assert client.put(f"{BASE}/{robot.id}/group", headers=ADMIN, json={"group_id": destination}).status_code == 200
        groups = {item["id"]: item for item in client.get(BASE + "/groups").json()["data"]}
        assert groups[study["id"]]["api_ids"] == ([robot.id] if destination == study["id"] else [])
        assert groups[robots["id"]]["api_ids"] == ([robot.id] if destination == robots["id"] else [])
    assert client.patch(f"{BASE}/groups/{robots['id']}", headers=ADMIN, json={"name": " Robot  QNick ", "collapsed": True}).status_code == 200
    renamed = next(item for item in client.get(BASE + "/groups").json()["data"] if item["id"] == robots["id"])
    assert renamed["name"] == "Robot QNick" and renamed["collapsed"]
    assert client.delete(f"{BASE}/groups/{robots['id']}", headers=ADMIN).status_code == 200
    assert client.portal.call(get_gateway_api, robot.id) == robot
    assert client.portal.call(authenticate_api_key, key).id == robot.api_key_id
    assert client.get(BASE + "/groups").json()["data"] == [study]


def test_invalid_memberships_and_duplicate_names_preserve_current_group(client):
    api, _ = profile(client, "WikiNick")
    study, robots = group(client, "Studio"), group(client, "Robot")
    assert client.put(f"{BASE}/{api.id}/group", headers=ADMIN, json={"group_id": study["id"]}).status_code == 200
    assert client.post(BASE + "/groups", headers=ADMIN, json={"name": "studio"}).status_code == 409
    assert client.patch(f"{BASE}/groups/{robots['id']}", headers=ADMIN, json={"name": "STUDIO"}).status_code == 409
    for payload in ({"name": " "}, {"name": "a" * 81}, {"name": "Valid", "api_ids": [api.id]}):
        assert client.post(BASE + "/groups", headers=ADMIN, json=payload).status_code == 400
    for payload in ({}, {"name": None}, {"collapsed": None}):
        assert client.patch(f"{BASE}/groups/{study['id']}", headers=ADMIN, json=payload).status_code == 400
    assert client.put(f"{BASE}/{api.id}/group", headers=ADMIN, json={"group_id": "missing"}).status_code == 404
    assert client.put(f"{BASE}/99999/group", headers=ADMIN, json={"group_id": study["id"]}).status_code == 404
    assert client.delete(BASE + "/groups/missing", headers=ADMIN).status_code == 404
    assert next(item for item in client.get(BASE + "/groups").json()["data"] if item["id"] == study["id"])["api_ids"] == [api.id]


def test_groups_require_dashboard_origin_and_operator_in_team_mode(client, monkeypatch):
    study = group(client, "Studio")
    assert client.post(BASE + "/groups", json={"name": "Blocked"}).status_code == 403
    evil = {**ADMIN, "Origin": "http://untrusted.example"}
    assert client.delete(f"{BASE}/groups/{study['id']}", headers=evil).status_code == 403
    operator = client.portal.call(partial(provision, "Group operator", operator=True))
    member = client.portal.call(partial(provision, "Group member"))
    monkeypatch.setattr(settings, "admin_auth_required", True)
    assert client.get(BASE + "/groups").status_code == 401
    assert client.get(BASE + "/groups", headers={"Authorization": "Bearer " + member["token"]}).status_code == 403
    authorized = {**ADMIN, "Authorization": "Bearer " + operator["token"]}
    assert client.get(BASE + "/groups", headers=authorized).status_code == 200
    assert client.patch(f"{BASE}/groups/{study['id']}", headers=authorized, json={"collapsed": True}).status_code == 200
