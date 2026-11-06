"""Dashboard-only organization of API profiles; no changes to credentials or grants."""

import sqlite3
import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.dashboard_security import require_dashboard_mutation
from app.database import _connect
from app.errors import GatewayError

router = APIRouter(prefix="/api/gateway-apis", include_in_schema=False)


class GroupName(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = " ".join(value.split())
        if not value:
            raise ValueError("Empty group name")
        return value


class GroupChange(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    collapsed: bool | None = None
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def valid_change(self):
        if not self.model_fields_set or any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Provide a group name or collapsed state")
        if self.name is not None:
            self.name = GroupName(name=self.name).name
        return self


class Membership(BaseModel):
    group_id: str | None = Field(max_length=40)
    model_config = ConfigDict(extra="forbid")


async def groups():
    async with _connect() as db:
        rows = await (await db.execute("SELECT id,name,collapsed FROM dashboard_api_groups ORDER BY name COLLATE NOCASE,id")).fetchall()
        members = await (await db.execute("SELECT m.group_id,m.gateway_api_id FROM dashboard_api_group_members m JOIN gateway_apis ga ON ga.id=m.gateway_api_id JOIN api_keys ak ON ak.id=ga.api_key_id WHERE ak.is_active=1 ORDER BY m.gateway_api_id")).fetchall()
    return [{"id": row["id"], "name": row["name"], "collapsed": bool(row["collapsed"]), "api_ids": [member["gateway_api_id"] for member in members if member["group_id"] == row["id"]]} for row in rows]


@router.get("/groups")
async def list_groups():
    return JSONResponse({"data": await groups()}, headers={"Cache-Control": "no-store"})


@router.post("/groups", dependencies=[Depends(require_dashboard_mutation)])
async def create_group(payload: GroupName):
    identifier = "grp_" + uuid.uuid4().hex
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        count = await (await db.execute("SELECT count(*) FROM dashboard_api_groups")).fetchone()
        if count[0] >= 100:
            raise GatewayError(429, "Limite di 100 gruppi raggiunto.", code="api_group_limit")
        try:
            await db.execute("INSERT INTO dashboard_api_groups(id,name,collapsed) VALUES (?,?,0)", (identifier, payload.name))
            await db.commit()
        except sqlite3.IntegrityError:
            raise GatewayError(409, "Esiste già un gruppo con questo nome.", code="api_group_conflict") from None
    return JSONResponse({"id": identifier, "name": payload.name, "collapsed": False, "api_ids": []}, status_code=201, headers={"Cache-Control": "no-store"})


@router.patch("/groups/{group_id}", dependencies=[Depends(require_dashboard_mutation)])
async def update_group(group_id: str, payload: GroupChange):
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute("SELECT * FROM dashboard_api_groups WHERE id=?", (group_id,))).fetchone()
        if row is None:
            raise GatewayError(404, "Gruppo non trovato.", code="api_group_not_found")
        try:
            await db.execute("UPDATE dashboard_api_groups SET name=?,collapsed=? WHERE id=?", (payload.name if payload.name is not None else row["name"], int(payload.collapsed) if payload.collapsed is not None else row["collapsed"], group_id))
            await db.commit()
        except sqlite3.IntegrityError:
            raise GatewayError(409, "Esiste già un gruppo con questo nome.", code="api_group_conflict") from None
    return JSONResponse({"status": "updated"}, headers={"Cache-Control": "no-store"})


@router.delete("/groups/{group_id}", dependencies=[Depends(require_dashboard_mutation)])
async def delete_group(group_id: str):
    async with _connect() as db:
        changed = await db.execute("DELETE FROM dashboard_api_groups WHERE id=?", (group_id,))
        if changed.rowcount != 1:
            raise GatewayError(404, "Gruppo non trovato.", code="api_group_not_found")
        await db.commit()
    return JSONResponse({"status": "deleted"}, headers={"Cache-Control": "no-store"})


@router.put("/{api_id}/group", dependencies=[Depends(require_dashboard_mutation)])
async def set_group(api_id: int, payload: Membership):
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        active = await (await db.execute("SELECT 1 FROM gateway_apis ga JOIN api_keys ak ON ak.id=ga.api_key_id WHERE ga.id=? AND ak.is_active=1", (api_id,))).fetchone()
        if active is None:
            raise GatewayError(404, "API non trovata.", code="gateway_api_not_found")
        if payload.group_id is None:
            await db.execute("DELETE FROM dashboard_api_group_members WHERE gateway_api_id=?", (api_id,))
        else:
            if await (await db.execute("SELECT 1 FROM dashboard_api_groups WHERE id=?", (payload.group_id,))).fetchone() is None:
                raise GatewayError(404, "Gruppo non trovato.", code="api_group_not_found")
            await db.execute("INSERT INTO dashboard_api_group_members(gateway_api_id,group_id) VALUES (?,?) ON CONFLICT(gateway_api_id) DO UPDATE SET group_id=excluded.group_id", (api_id, payload.group_id))
        await db.commit()
    return JSONResponse({"api_id": api_id, "group_id": payload.group_id}, headers={"Cache-Control": "no-store"})
