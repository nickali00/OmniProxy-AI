"""Operator-only portable templates. No HTTP endpoints or credential creation.

Export/import is an explicit local CLI operation. Import stores reviewed
templates separately; it never activates profiles, grants or MCP servers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas import ApiLimits, SkillConfig


class McpTemplate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    url: str = Field(min_length=8, max_length=2048)
    auth_type: Literal["none", "bearer"] = "none"
    model_config = ConfigDict(extra="forbid")

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("URL with secrets or unsupported scheme")
        return value


class ToolReference(BaseModel):
    server: str = Field(min_length=2, max_length=80)
    tools: list[str] = Field(max_length=100)
    model_config = ConfigDict(extra="forbid")

    @field_validator("tools")
    @classmethod
    def valid_tools(cls, values):
        if any(not value or len(value) > 128 or not all(char.isalnum() or char in "._:-" for char in value) for value in values):
            raise ValueError("Invalid tool name")
        return list(dict.fromkeys(values))


class ProfileTemplate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    provider: Literal["ollama", "codex", "claude", "gemini", "openai_compatible"]
    model: str = Field(min_length=1, max_length=128)
    reasoning_effort: str = Field(min_length=1, max_length=32)
    skills: list[str] = Field(default_factory=list, max_length=50)
    mcp_tools: list[ToolReference] = Field(default_factory=list, max_length=20)
    limits: ApiLimits = Field(default_factory=ApiLimits)
    model_config = ConfigDict(extra="forbid")


class Bundle(BaseModel):
    format: Literal["omniproxy.configuration"] = "omniproxy.configuration"
    version: Literal[1] = 1
    skills: list[SkillConfig] = Field(default_factory=list, max_length=100)
    mcp_servers: list[McpTemplate] = Field(default_factory=list, max_length=100)
    profiles: list[ProfileTemplate] = Field(default_factory=list, max_length=100)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_graph(self):
        for collection in (self.skills, self.mcp_servers, self.profiles):
            if len({item.name.casefold() for item in collection}) != len(collection):
                raise ValueError("Duplicate name")
        skills = {item.name for item in self.skills}
        servers = {item.name for item in self.mcp_servers}
        if any(name not in skills for profile in self.profiles for name in profile.skills) or any(binding.server not in servers for profile in self.profiles for binding in profile.mcp_tools):
            raise ValueError("Missing reference")
        if len(self.model_dump_json().encode()) > 1_000_000:
            raise ValueError("Bundle too large")
        return self

    def canonical(self):
        return json.dumps(self.model_dump(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def fingerprint(self):
        return hashlib.sha256(self.canonical().encode()).hexdigest()

    def plan(self):
        return {"approval_hash": self.fingerprint(), "profiles": len(self.profiles), "skills": len(self.skills), "mcp_servers": len(self.mcp_servers), "state": "templates_only", "credentials_included": False, "workspace_grants_included": False}


def export_bundle(database: Path):
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        skills = db.execute("SELECT name,description,instructions,enabled FROM skills ORDER BY id").fetchall()
        servers = db.execute("SELECT name,url,auth_type FROM mcp_servers ORDER BY id").fetchall()
        profiles = db.execute("SELECT ga.*,ak.limits_json FROM gateway_apis ga JOIN api_keys ak ON ak.id=ga.api_key_id WHERE ak.is_active=1 ORDER BY ga.id").fetchall()
        templates = []
        for row in profiles:
            linked = db.execute("SELECT s.name FROM skills s JOIN gateway_api_skills l ON l.skill_id=s.id WHERE l.gateway_api_id=? ORDER BY l.position", (row["id"],)).fetchall()
            tools = db.execute("SELECT s.name,l.tool_name FROM mcp_servers s JOIN gateway_api_mcp_tools l ON l.mcp_server_id=s.id WHERE l.gateway_api_id=? ORDER BY s.id,l.tool_name", (row["id"],)).fetchall()
            grouped = {}
            for item in tools:
                grouped.setdefault(item["name"], []).append(item["tool_name"])
            templates.append({**{key: row[key] for key in ("name", "provider", "model", "reasoning_effort")}, "limits": json.loads(row["limits_json"]), "skills": [item["name"] for item in linked], "mcp_tools": [{"server": name, "tools": names} for name, names in grouped.items()]})
    return Bundle(skills=[dict(row) for row in skills], mcp_servers=[dict(row) for row in servers], profiles=templates)


def write_private(path: Path, content: str):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def import_templates(database: Path, bundle: Bundle, approval_hash: str):
    if approval_hash != bundle.fingerprint():
        raise ValueError("Review the exact bundle before import")
    # Import goes into a separate inert registry, never into api_keys,
    # gateway_apis, workspaces, permissions, skills, or active MCP servers.
    with sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS configuration_templates (fingerprint TEXT PRIMARY KEY, data_json TEXT NOT NULL, created_at REAL NOT NULL)")
        db.execute("INSERT OR IGNORE INTO configuration_templates VALUES (?,?,?)", (bundle.fingerprint(), bundle.canonical(), time.time()))
    return bundle.plan()


def read_bundle(path: Path):
    with path.open("rb") as source:
        data = source.read(1_000_001)
    if len(data) > 1_000_000:
        raise ValueError("Bundle too large")
    return Bundle.model_validate_json(data)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    export = sub.add_parser("export")
    export.add_argument("--database", required=True, type=Path)
    export.add_argument("--output", required=True, type=Path)
    check = sub.add_parser("check")
    check.add_argument("--input", required=True, type=Path)
    apply = sub.add_parser("import")
    apply.add_argument("--database", required=True, type=Path)
    apply.add_argument("--input", required=True, type=Path)
    apply.add_argument("--approval-hash", required=True)
    args = parser.parse_args()
    try:
        if args.action == "export":
            bundle = export_bundle(args.database)
            write_private(args.output, bundle.model_dump_json(indent=2) + "\n")
            result = bundle.plan()
        else:
            bundle = read_bundle(args.input)
            result = bundle.plan() if args.action == "check" else import_templates(args.database, bundle, args.approval_hash)
        print(json.dumps(result))
    except (OSError, ValueError, sqlite3.Error):
        raise SystemExit("Operazione bundle non riuscita: verifica formato, percorso, permessi e hash. Nessuna credenziale viene creata.") from None


if __name__ == "__main__":
    main()
