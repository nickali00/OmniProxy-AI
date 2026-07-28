from __future__ import annotations

import hashlib
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from app.config import settings


API_KEY_PREFIX = "sk-local-"


@dataclass(frozen=True)
class ApiKeyRecord:
    id: int
    name: str
    key_hint: str


@dataclass(frozen=True)
class GatewayApiRecord:
    id: int
    name: str
    slug: str
    api_key_id: int
    key_hint: str
    status: str
    provider: str
    model: str
    reasoning_effort: str
    created_at: str
    updated_at: str
    last_used_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "key_hint": self.key_hint,
            "api_key_id": self.api_key_id,
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
        }


@dataclass(frozen=True)
class ProviderConnectionRecord:
    provider: str
    configuration_ciphertext: str
    tokens_ciphertext: str | None
    connected_at: str | None
    updated_at: str


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_hint TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'paused')
    ),
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS usage_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    api_key_id INTEGER NOT NULL,
    requested_model TEXT NOT NULL,
    routed_provider TEXT NOT NULL,
    resolved_model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    status_code INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
);

CREATE TABLE IF NOT EXISTS gateway_apis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    slug TEXT NOT NULL COLLATE NOCASE UNIQUE,
    api_key_id INTEGER NOT NULL UNIQUE,
    provider TEXT NOT NULL CHECK (
        provider IN ('ollama', 'codex', 'gemini', 'claude')
    ),
    model TEXT NOT NULL,
    reasoning_effort TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    updated_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
);

CREATE TABLE IF NOT EXISTS provider_connections (
    provider TEXT PRIMARY KEY CHECK (provider IN ('gemini')),
    configuration_ciphertext TEXT NOT NULL,
    tokens_ciphertext TEXT,
    connected_at TEXT,
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    updated_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE TABLE IF NOT EXISTS build_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    folder_name TEXT NOT NULL DEFAULT '',
    idea TEXT NOT NULL DEFAULT '',
    analyst_mode TEXT NOT NULL DEFAULT 'detailed' CHECK (
        analyst_mode IN ('schematic', 'detailed')
    ),
    analyst_provider TEXT NOT NULL CHECK (
        analyst_provider IN ('ollama', 'codex', 'gemini', 'claude')
    ),
    analyst_model TEXT NOT NULL,
    analyst_reasoning_effort TEXT NOT NULL,
    builder_provider TEXT NOT NULL CHECK (
        builder_provider IN ('ollama', 'codex', 'gemini', 'claude')
    ),
    builder_model TEXT NOT NULL,
    builder_reasoning_effort TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    updated_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE TABLE IF NOT EXISTS build_project_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    updated_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    UNIQUE(project_id, path),
    FOREIGN KEY (project_id) REFERENCES build_projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS build_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    artifact_type TEXT NOT NULL CHECK (
        artifact_type IN (
            'analysis',
            'builder_brief',
            'roadmap',
            'future_features'
        )
    ),
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    UNIQUE(project_id, artifact_type),
    FOREIGN KEY (project_id) REFERENCES build_projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS build_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    lane TEXT NOT NULL CHECK (lane IN ('analyst', 'builder')),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    message_type TEXT NOT NULL DEFAULT 'chat' CHECK (
        message_type IN ('chat', 'handoff')
    ),
    source_message_id INTEGER,
    content TEXT NOT NULL,
    changes_applied_at TEXT,
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    FOREIGN KEY (project_id) REFERENCES build_projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS build_phases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position > 0),
    title TEXT NOT NULL,
    instruction TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN (
            'pending',
            'running',
            'awaiting_apply',
            'completed',
            'blocked'
        )
    ),
    source_message_id INTEGER,
    builder_message_id INTEGER,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    updated_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    UNIQUE(project_id, position),
    FOREIGN KEY (project_id) REFERENCES build_projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_api_keys_active_hash
    ON api_keys(key_hash, is_active);
CREATE INDEX IF NOT EXISTS idx_usage_logs_api_key_created
    ON usage_logs(api_key_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_logs_model_created
    ON usage_logs(requested_model, created_at);
CREATE INDEX IF NOT EXISTS idx_gateway_apis_provider_model
    ON gateway_apis(provider, model);
CREATE INDEX IF NOT EXISTS idx_build_files_project_path
    ON build_project_files(project_id, path);
CREATE INDEX IF NOT EXISTS idx_build_artifacts_project
    ON build_artifacts(project_id, artifact_type);
CREATE INDEX IF NOT EXISTS idx_build_messages_project_created
    ON build_messages(project_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_build_phases_project_position
    ON build_phases(project_id, position);
CREATE UNIQUE INDEX IF NOT EXISTS idx_build_phases_builder_message
    ON build_phases(builder_message_id)
    WHERE builder_message_id IS NOT NULL;
"""


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def validate_api_key_format(raw_key: str) -> None:
    if not raw_key.startswith(API_KEY_PREFIX) or len(raw_key) < 32:
        raise ValueError(
            "A local API key must start with 'sk-local-' and contain "
            "at least 32 characters."
        )


def _key_hint(raw_key: str) -> str:
    return f"{raw_key[:13]}...{raw_key[-4:]}"


def _clean_gateway_api_name(name: str) -> str:
    clean_name = " ".join(name.strip().split())
    if not 2 <= len(clean_name) <= 80:
        raise ValueError("Il nome dell'API deve contenere da 2 a 80 caratteri.")
    return clean_name


def _slug_base(name: str) -> str:
    normalized = name.lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    if not normalized:
        normalized = "api"
    return normalized[:48].rstrip("-")


def _gateway_api_from_row(row: aiosqlite.Row) -> GatewayApiRecord:
    return GatewayApiRecord(
        id=int(row["id"]),
        name=str(row["name"]),
        slug=str(row["slug"]),
        api_key_id=int(row["api_key_id"]),
        key_hint=str(row["key_hint"]),
        status=str(row["status"]),
        provider=str(row["provider"]),
        model=str(row["model"]),
        reasoning_effort=str(row["reasoning_effort"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        last_used_at=(
            str(row["last_used_at"])
            if row["last_used_at"] is not None
            else None
        ),
    )


@asynccontextmanager
async def _connect() -> AsyncIterator[aiosqlite.Connection]:
    connection = await aiosqlite.connect(str(settings.database_path))
    connection.row_factory = aiosqlite.Row
    await connection.execute("PRAGMA foreign_keys = ON")
    await connection.execute("PRAGMA busy_timeout = 5000")
    try:
        yield connection
    finally:
        await connection.close()


async def init_database() -> None:
    database_path = Path(settings.database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    async with _connect() as connection:
        await connection.execute("PRAGMA journal_mode = WAL")
        await connection.execute("PRAGMA synchronous = NORMAL")
        await connection.executescript(SCHEMA_SQL)
        api_key_columns = await connection.execute(
            "PRAGMA table_info(api_keys)"
        )
        api_key_column_names = {
            str(row["name"]) for row in await api_key_columns.fetchall()
        }
        if "status" not in api_key_column_names:
            await connection.execute(
                """
                ALTER TABLE api_keys
                ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'paused'))
                """
            )
        project_columns = await connection.execute(
            "PRAGMA table_info(build_projects)"
        )
        project_column_names = {
            str(row["name"]) for row in await project_columns.fetchall()
        }
        if "analyst_mode" not in project_column_names:
            await connection.execute(
                """
                ALTER TABLE build_projects
                ADD COLUMN analyst_mode TEXT NOT NULL DEFAULT 'detailed'
                CHECK (analyst_mode IN ('schematic', 'detailed'))
                """
            )
        columns = await connection.execute(
            "PRAGMA table_info(build_messages)"
        )
        column_names = {
            str(row["name"]) for row in await columns.fetchall()
        }
        if "message_type" not in column_names:
            await connection.execute(
                """
                ALTER TABLE build_messages
                ADD COLUMN message_type TEXT NOT NULL DEFAULT 'chat'
                CHECK (message_type IN ('chat', 'handoff'))
                """
            )
        if "source_message_id" not in column_names:
            await connection.execute(
                """
                ALTER TABLE build_messages
                ADD COLUMN source_message_id INTEGER
                """
            )
        if "changes_applied_at" not in column_names:
            await connection.execute(
                """
                ALTER TABLE build_messages
                ADD COLUMN changes_applied_at TEXT
                """
            )
        await connection.commit()


async def ping_database() -> bool:
    try:
        async with _connect() as connection:
            cursor = await connection.execute("SELECT 1")
            row = await cursor.fetchone()
            return bool(row and row[0] == 1)
    except aiosqlite.Error:
        return False


async def get_provider_connection(
    provider: str,
) -> ProviderConnectionRecord | None:
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            SELECT
                provider,
                configuration_ciphertext,
                tokens_ciphertext,
                connected_at,
                updated_at
            FROM provider_connections
            WHERE provider = ?
            LIMIT 1
            """,
            (provider,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return ProviderConnectionRecord(
        provider=str(row["provider"]),
        configuration_ciphertext=str(row["configuration_ciphertext"]),
        tokens_ciphertext=(
            str(row["tokens_ciphertext"])
            if row["tokens_ciphertext"] is not None
            else None
        ),
        connected_at=(
            str(row["connected_at"])
            if row["connected_at"] is not None
            else None
        ),
        updated_at=str(row["updated_at"]),
    )


async def save_provider_configuration(
    provider: str,
    configuration_ciphertext: str,
) -> ProviderConnectionRecord:
    async with _connect() as connection:
        await connection.execute(
            """
            INSERT INTO provider_connections(
                provider,
                configuration_ciphertext,
                tokens_ciphertext,
                connected_at
            )
            VALUES (?, ?, NULL, NULL)
            ON CONFLICT(provider) DO UPDATE SET
                configuration_ciphertext = excluded.configuration_ciphertext,
                tokens_ciphertext = NULL,
                connected_at = NULL,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (provider, configuration_ciphertext),
        )
        await connection.commit()
    record = await get_provider_connection(provider)
    if record is None:
        raise RuntimeError("Provider configuration was not persisted.")
    return record


async def save_provider_tokens(
    provider: str,
    tokens_ciphertext: str,
) -> None:
    async with _connect() as connection:
        await connection.execute(
            """
            UPDATE provider_connections
            SET
                tokens_ciphertext = ?,
                connected_at = COALESCE(
                    connected_at,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                ),
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE provider = ?
            """,
            (tokens_ciphertext, provider),
        )
        await connection.commit()


async def clear_provider_tokens(provider: str) -> None:
    async with _connect() as connection:
        await connection.execute(
            """
            UPDATE provider_connections
            SET
                tokens_ciphertext = NULL,
                connected_at = NULL,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE provider = ?
            """,
            (provider,),
        )
        await connection.commit()


async def create_api_key(
    name: str,
    raw_key: str | None = None,
) -> tuple[ApiKeyRecord, str]:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("API key name cannot be empty.")

    secret = raw_key or generate_api_key()
    validate_api_key_format(secret)

    async with _connect() as connection:
        cursor = await connection.execute(
            """
            INSERT INTO api_keys(name, key_hint, key_hash)
            VALUES (?, ?, ?)
            """,
            (clean_name, _key_hint(secret), hash_api_key(secret)),
        )
        await connection.commit()
        record = ApiKeyRecord(
            id=int(cursor.lastrowid),
            name=clean_name,
            key_hint=_key_hint(secret),
        )
    return record, secret


async def ensure_bootstrap_api_key() -> None:
    raw_key = settings.bootstrap_api_key.strip()
    if not raw_key:
        return

    validate_api_key_format(raw_key)
    key_hash = hash_api_key(raw_key)
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            SELECT id, key_hash
            FROM api_keys
            WHERE name = 'bootstrap'
            ORDER BY id
            LIMIT 1
            """
        )
        existing = await cursor.fetchone()
        if existing is None:
            await connection.execute(
                """
                INSERT INTO api_keys(name, key_hint, key_hash)
                VALUES (?, ?, ?)
                """,
                ("bootstrap", _key_hint(raw_key), key_hash),
            )
        elif existing["key_hash"] != key_hash:
            # Cambiare BOOTSTRAP_API_KEY ruota la chiave iniziale: il vecchio
            # segreto non resta accidentalmente valido nel database.
            await connection.execute(
                """
                UPDATE api_keys
                SET key_hint = ?, key_hash = ?, is_active = 1,
                    status = 'active'
                WHERE id = ?
                """,
                (_key_hint(raw_key), key_hash, existing["id"]),
            )
        await connection.commit()


async def authenticate_api_key(raw_key: str) -> ApiKeyRecord | None:
    if not raw_key.startswith(API_KEY_PREFIX):
        return None

    async with _connect() as connection:
        cursor = await connection.execute(
            """
            SELECT id, name, key_hint
            FROM api_keys
            WHERE key_hash = ?
              AND is_active = 1
              AND status = 'active'
            LIMIT 1
            """,
            (hash_api_key(raw_key),),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        await connection.execute(
            """
            UPDATE api_keys
            SET last_used_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (row["id"],),
        )
        await connection.commit()
        return ApiKeyRecord(
            id=int(row["id"]),
            name=str(row["name"]),
            key_hint=str(row["key_hint"]),
        )


async def list_api_keys() -> list[dict[str, object]]:
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            SELECT id, name, key_hint, is_active, status, created_at, last_used_at
            FROM api_keys
            ORDER BY id
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def revoke_api_key(key_id: int) -> bool:
    async with _connect() as connection:
        cursor = await connection.execute(
            "UPDATE api_keys SET is_active = 0 WHERE id = ? AND is_active = 1",
            (key_id,),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def toggle_api_key_status(
    key_id: int,
) -> dict[str, object] | None:
    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            """
            UPDATE api_keys
            SET status = CASE status
                WHEN 'active' THEN 'paused'
                ELSE 'active'
            END
            WHERE id = ? AND is_active = 1
            """,
            (key_id,),
        )
        if cursor.rowcount != 1:
            await connection.rollback()
            return None
        result = await connection.execute(
            """
            SELECT id, name, key_hint, status, created_at, last_used_at
            FROM api_keys
            WHERE id = ?
            """,
            (key_id,),
        )
        row = await result.fetchone()
        await connection.commit()
    return dict(row) if row is not None else None


async def create_gateway_api(
    *,
    name: str,
    provider: str,
    model: str,
    reasoning_effort: str,
) -> tuple[GatewayApiRecord, str]:
    clean_name = _clean_gateway_api_name(name)
    secret = generate_api_key()
    key_hint = _key_hint(secret)

    async with _connect() as connection:
        try:
            await connection.execute("BEGIN IMMEDIATE")
            key_cursor = await connection.execute(
                """
                INSERT INTO api_keys(name, key_hint, key_hash)
                VALUES (?, ?, ?)
                """,
                (f"api:{clean_name}", key_hint, hash_api_key(secret)),
            )
            api_key_id = int(key_cursor.lastrowid)
            slug_base = _slug_base(clean_name)
            slug = slug_base
            suffix = 1
            while True:
                cursor = await connection.execute(
                    "SELECT 1 FROM gateway_apis WHERE slug = ? COLLATE NOCASE",
                    (slug,),
                )
                if await cursor.fetchone() is None:
                    break
                suffix += 1
                slug = f"{slug_base[:42]}-{suffix}"

            api_cursor = await connection.execute(
                """
                INSERT INTO gateway_apis(
                    name, slug, api_key_id, provider, model, reasoning_effort
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_name,
                    slug,
                    api_key_id,
                    provider,
                    model,
                    reasoning_effort,
                ),
            )
            api_id = int(api_cursor.lastrowid)
            cursor = await connection.execute(
                """
                SELECT ga.*, ak.key_hint, ak.status, ak.last_used_at
                FROM gateway_apis AS ga
                JOIN api_keys AS ak ON ak.id = ga.api_key_id
                WHERE ga.id = ?
                """,
                (api_id,),
            )
            row = await cursor.fetchone()
            await connection.commit()
        except aiosqlite.IntegrityError as exc:
            await connection.rollback()
            raise ValueError("Esiste già un'API con questo nome.") from exc

    if row is None:
        raise RuntimeError("Impossibile rileggere l'API appena creata.")
    return _gateway_api_from_row(row), secret


async def list_gateway_apis() -> list[GatewayApiRecord]:
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            SELECT ga.*, ak.key_hint, ak.status, ak.last_used_at
            FROM gateway_apis AS ga
            JOIN api_keys AS ak ON ak.id = ga.api_key_id
            WHERE ak.is_active = 1
            ORDER BY ga.created_at DESC, ga.id DESC
            """
        )
        rows = await cursor.fetchall()
    return [_gateway_api_from_row(row) for row in rows]


async def get_gateway_api(api_id: int) -> GatewayApiRecord | None:
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            SELECT ga.*, ak.key_hint, ak.status, ak.last_used_at
            FROM gateway_apis AS ga
            JOIN api_keys AS ak ON ak.id = ga.api_key_id
            WHERE ga.id = ? AND ak.is_active = 1
            LIMIT 1
            """,
            (api_id,),
        )
        row = await cursor.fetchone()
    return _gateway_api_from_row(row) if row is not None else None


async def get_gateway_api_for_key(
    api_key_id: int,
) -> GatewayApiRecord | None:
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            SELECT ga.*, ak.key_hint, ak.status, ak.last_used_at
            FROM gateway_apis AS ga
            JOIN api_keys AS ak ON ak.id = ga.api_key_id
            WHERE ga.api_key_id = ? AND ak.is_active = 1
            LIMIT 1
            """,
            (api_key_id,),
        )
        row = await cursor.fetchone()
    return _gateway_api_from_row(row) if row is not None else None


async def update_gateway_api(
    api_id: int,
    *,
    name: str,
    provider: str,
    model: str,
    reasoning_effort: str,
) -> GatewayApiRecord | None:
    clean_name = _clean_gateway_api_name(name)
    async with _connect() as connection:
        try:
            cursor = await connection.execute(
                """
                UPDATE gateway_apis
                SET name = ?,
                    provider = ?,
                    model = ?,
                    reasoning_effort = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (
                    clean_name,
                    provider,
                    model,
                    reasoning_effort,
                    api_id,
                ),
            )
            if cursor.rowcount != 1:
                await connection.rollback()
                return None
            await connection.execute(
                """
                UPDATE api_keys
                SET name = ?
                WHERE id = (SELECT api_key_id FROM gateway_apis WHERE id = ?)
                """,
                (f"api:{clean_name}", api_id),
            )
            result = await connection.execute(
                """
                SELECT ga.*, ak.key_hint, ak.status, ak.last_used_at
                FROM gateway_apis AS ga
                JOIN api_keys AS ak ON ak.id = ga.api_key_id
                WHERE ga.id = ? AND ak.is_active = 1
                """,
                (api_id,),
            )
            row = await result.fetchone()
            await connection.commit()
        except aiosqlite.IntegrityError as exc:
            await connection.rollback()
            raise ValueError("Esiste già un'API con questo nome.") from exc
    return _gateway_api_from_row(row) if row is not None else None


async def delete_gateway_api(api_id: int) -> bool:
    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            "SELECT api_key_id FROM gateway_apis WHERE id = ?",
            (api_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            await connection.rollback()
            return False
        api_key_id = int(row["api_key_id"])
        await connection.execute(
            "UPDATE api_keys SET is_active = 0 WHERE id = ?",
            (api_key_id,),
        )
        await connection.execute(
            "DELETE FROM gateway_apis WHERE id = ?",
            (api_id,),
        )
        await connection.commit()
        return True


def _build_project_from_row(row: aiosqlite.Row) -> dict[str, object]:
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "folder_name": str(row["folder_name"]),
        "idea": str(row["idea"]),
        "analyst_mode": str(row["analyst_mode"]),
        "analyst": {
            "provider": str(row["analyst_provider"]),
            "model": str(row["analyst_model"]),
            "reasoning_effort": str(row["analyst_reasoning_effort"]),
        },
        "builder": {
            "provider": str(row["builder_provider"]),
            "model": str(row["builder_model"]),
            "reasoning_effort": str(row["builder_reasoning_effort"]),
        },
        "file_count": int(row["file_count"]),
        "total_bytes": int(row["total_bytes"]),
        "artifact_count": int(row["artifact_count"]),
        "message_count": int(row["message_count"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


async def _read_build_project(
    connection: aiosqlite.Connection,
    project_id: int,
) -> aiosqlite.Row | None:
    cursor = await connection.execute(
        """
        SELECT
            bp.*,
            COUNT(DISTINCT bpf.id) AS file_count,
            COALESCE(MAX(file_totals.total_bytes), 0) AS total_bytes,
            COUNT(DISTINCT ba.id) AS artifact_count,
            COUNT(DISTINCT bm.id) AS message_count
        FROM build_projects AS bp
        LEFT JOIN build_project_files AS bpf ON bpf.project_id = bp.id
        LEFT JOIN (
            SELECT project_id, SUM(size_bytes) AS total_bytes
            FROM build_project_files
            GROUP BY project_id
        ) AS file_totals ON file_totals.project_id = bp.id
        LEFT JOIN build_artifacts AS ba ON ba.project_id = bp.id
        LEFT JOIN build_messages AS bm ON bm.project_id = bp.id
        WHERE bp.id = ?
        GROUP BY bp.id
        """,
        (project_id,),
    )
    return await cursor.fetchone()


async def _insert_build_files(
    connection: aiosqlite.Connection,
    project_id: int,
    files: list[dict[str, str]],
) -> None:
    for item in files:
        content = item["content"]
        encoded = content.encode("utf-8")
        await connection.execute(
            """
            INSERT INTO build_project_files(
                project_id, path, content, content_sha256, size_bytes
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                project_id,
                item["path"],
                content,
                hashlib.sha256(encoded).hexdigest(),
                len(encoded),
            ),
        )


async def create_build_project(
    *,
    name: str,
    folder_name: str,
    idea: str,
    analyst_mode: str,
    analyst_provider: str,
    analyst_model: str,
    analyst_reasoning_effort: str,
    builder_provider: str,
    builder_model: str,
    builder_reasoning_effort: str,
    files: list[dict[str, str]],
) -> dict[str, object]:
    clean_name = _clean_gateway_api_name(name)
    async with _connect() as connection:
        try:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                """
                INSERT INTO build_projects(
                    name,
                    folder_name,
                    idea,
                    analyst_mode,
                    analyst_provider,
                    analyst_model,
                    analyst_reasoning_effort,
                    builder_provider,
                    builder_model,
                    builder_reasoning_effort
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_name,
                    folder_name,
                    idea,
                    analyst_mode,
                    analyst_provider,
                    analyst_model,
                    analyst_reasoning_effort,
                    builder_provider,
                    builder_model,
                    builder_reasoning_effort,
                ),
            )
            project_id = int(cursor.lastrowid)
            await _insert_build_files(connection, project_id, files)
            row = await _read_build_project(connection, project_id)
            await connection.commit()
        except aiosqlite.IntegrityError as exc:
            await connection.rollback()
            raise ValueError(
                "Esiste già un progetto Build con questo nome."
            ) from exc
    if row is None:
        raise RuntimeError("Impossibile rileggere il progetto appena creato.")
    return _build_project_from_row(row)


async def list_build_projects() -> list[dict[str, object]]:
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            SELECT id
            FROM build_projects
            ORDER BY updated_at DESC, id DESC
            """
        )
        ids = [int(row["id"]) for row in await cursor.fetchall()]
        rows = [
            await _read_build_project(connection, project_id)
            for project_id in ids
        ]
    return [
        _build_project_from_row(row)
        for row in rows
        if row is not None
    ]


async def get_build_project(project_id: int) -> dict[str, object] | None:
    async with _connect() as connection:
        row = await _read_build_project(connection, project_id)
    return _build_project_from_row(row) if row is not None else None


async def update_build_project(
    project_id: int,
    *,
    name: str,
    folder_name: str,
    idea: str,
    analyst_mode: str,
    analyst_provider: str,
    analyst_model: str,
    analyst_reasoning_effort: str,
    builder_provider: str,
    builder_model: str,
    builder_reasoning_effort: str,
) -> dict[str, object] | None:
    clean_name = _clean_gateway_api_name(name)
    async with _connect() as connection:
        try:
            cursor = await connection.execute(
                """
                UPDATE build_projects
                SET name = ?,
                    folder_name = ?,
                    idea = ?,
                    analyst_mode = ?,
                    analyst_provider = ?,
                    analyst_model = ?,
                    analyst_reasoning_effort = ?,
                    builder_provider = ?,
                    builder_model = ?,
                    builder_reasoning_effort = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (
                    clean_name,
                    folder_name,
                    idea,
                    analyst_mode,
                    analyst_provider,
                    analyst_model,
                    analyst_reasoning_effort,
                    builder_provider,
                    builder_model,
                    builder_reasoning_effort,
                    project_id,
                ),
            )
            if cursor.rowcount != 1:
                await connection.rollback()
                return None
            row = await _read_build_project(connection, project_id)
            await connection.commit()
        except aiosqlite.IntegrityError as exc:
            await connection.rollback()
            raise ValueError(
                "Esiste già un progetto Build con questo nome."
            ) from exc
    return _build_project_from_row(row) if row is not None else None


async def replace_build_project_files(
    project_id: int,
    *,
    folder_name: str,
    files: list[dict[str, str]],
) -> dict[str, object] | None:
    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            """
            UPDATE build_projects
            SET folder_name = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (folder_name, project_id),
        )
        if cursor.rowcount != 1:
            await connection.rollback()
            return None
        await connection.execute(
            "DELETE FROM build_project_files WHERE project_id = ?",
            (project_id,),
        )
        await _insert_build_files(connection, project_id, files)
        row = await _read_build_project(connection, project_id)
        await connection.commit()
    return _build_project_from_row(row) if row is not None else None


async def delete_build_project(project_id: int) -> bool:
    async with _connect() as connection:
        cursor = await connection.execute(
            "DELETE FROM build_projects WHERE id = ?",
            (project_id,),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def list_build_project_files(
    project_id: int,
) -> list[dict[str, object]]:
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            SELECT path, content_sha256, size_bytes, updated_at
            FROM build_project_files
            WHERE project_id = ?
            ORDER BY path COLLATE NOCASE
            """,
            (project_id,),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def list_build_project_file_contents(
    project_id: int,
) -> list[dict[str, object]]:
    """Return file contents for internal Builder patch validation only."""
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            SELECT path, content, content_sha256, size_bytes, updated_at
            FROM build_project_files
            WHERE project_id = ?
            ORDER BY path COLLATE NOCASE
            """,
            (project_id,),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def build_project_context(
    project_id: int,
    *,
    max_characters: int = 48_000,
    preferred_paths: list[str] | None = None,
    preferred_only: bool = False,
) -> str:
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            SELECT path, content
            FROM build_project_files
            WHERE project_id = ?
            ORDER BY
                CASE
                    WHEN lower(path) LIKE '%readme%' THEN 0
                    WHEN lower(path) LIKE '%pyproject.toml' THEN 1
                    WHEN lower(path) LIKE '%package.json' THEN 1
                    WHEN lower(path) LIKE '%docker-compose.y%ml' THEN 2
                    ELSE 3
                END,
                path COLLATE NOCASE
            """,
            (project_id,),
        )
        rows = await cursor.fetchall()

    all_paths = [str(row["path"]) for row in rows]
    preferred_order = {
        path.replace("\\", "/").casefold(): index
        for index, path in enumerate(preferred_paths or [])
    }
    if preferred_order:
        rows = sorted(
            rows,
            key=lambda row: (
                preferred_order.get(
                    str(row["path"]).replace("\\", "/").casefold(),
                    len(preferred_order),
                ),
                str(row["path"]).casefold(),
            ),
        )
        if preferred_only:
            rows = [
                row
                for row in rows
                if str(row["path"])
                .replace("\\", "/")
                .casefold() in preferred_order
            ]

    manifest_budget = min(60_000, max(8_000, max_characters // 4))
    manifest_lines = ["\n--- MANIFEST FILE INDICIZZATI ---"]
    manifest_size = len(manifest_lines[0])
    for path_index, path in enumerate(all_paths):
        line = f"\n{path}"
        if manifest_size + len(line) > manifest_budget:
            manifest_lines.append(
                f"\n[… altri {len(all_paths) - path_index} file]"
            )
            break
        manifest_lines.append(line)
        manifest_size += len(line)
    manifest_lines.append("\n--- FINE MANIFEST ---\n")
    manifest = "".join(manifest_lines)
    blocks: list[str] = [manifest]
    used = len(manifest)
    for row in rows:
        block = f"\n--- {row['path']} ---\n{row['content']}\n"
        remaining = max_characters - used
        if remaining <= 0:
            break
        if len(block) <= remaining:
            blocks.append(block)
            used += len(block)
            continue
        truncation = "\n[FILE TRONCATO: non generare patch per questo file]\n"
        available = max(0, remaining - len(truncation))
        blocks.append(f"{block[:available]}{truncation}")
        used = max_characters
    return "".join(blocks)


async def replace_build_artifacts(
    project_id: int,
    artifacts: list[tuple[str, str]],
) -> list[dict[str, object]]:
    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        await connection.execute(
            "DELETE FROM build_artifacts WHERE project_id = ?",
            (project_id,),
        )
        for artifact_type, content in artifacts:
            await connection.execute(
                """
                INSERT INTO build_artifacts(project_id, artifact_type, content)
                VALUES (?, ?, ?)
                """,
                (project_id, artifact_type, content),
            )
        await connection.execute(
            """
            UPDATE build_projects
            SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (project_id,),
        )
        await connection.commit()
    return await list_build_artifacts(project_id)


async def list_build_artifacts(
    project_id: int,
) -> list[dict[str, object]]:
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            SELECT id, artifact_type, content, updated_at
            FROM build_artifacts
            WHERE project_id = ?
            ORDER BY CASE artifact_type
                WHEN 'analysis' THEN 1
                WHEN 'builder_brief' THEN 2
                WHEN 'roadmap' THEN 3
                WHEN 'future_features' THEN 4
            END
            """,
            (project_id,),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def add_build_message(
    project_id: int,
    *,
    lane: str,
    role: str,
    content: str,
    message_type: str = "chat",
    source_message_id: int | None = None,
) -> dict[str, object]:
    if message_type not in {"chat", "handoff"}:
        raise ValueError("Tipo di messaggio Build non valido.")
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            INSERT INTO build_messages(
                project_id,
                lane,
                role,
                message_type,
                source_message_id,
                content
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                lane,
                role,
                message_type,
                source_message_id,
                content,
            ),
        )
        message_id = int(cursor.lastrowid)
        await connection.execute(
            """
            UPDATE build_projects
            SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (project_id,),
        )
        result = await connection.execute(
            """
            SELECT
                id,
                lane,
                role,
                message_type,
                source_message_id,
                content,
                changes_applied_at,
                created_at
            FROM build_messages
            WHERE id = ?
            """,
            (message_id,),
        )
        row = await result.fetchone()
        await connection.commit()
    if row is None:
        raise RuntimeError("Impossibile rileggere il messaggio Build.")
    return dict(row)


async def list_build_messages(
    project_id: int,
    *,
    limit: int = 80,
) -> list[dict[str, object]]:
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            SELECT
                id,
                lane,
                role,
                message_type,
                source_message_id,
                content,
                changes_applied_at,
                created_at
            FROM (
                SELECT
                    id,
                    lane,
                    role,
                    message_type,
                    source_message_id,
                    content,
                    changes_applied_at,
                    created_at
                FROM build_messages
                WHERE project_id = ?
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY id
            """,
            (project_id, limit),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def list_build_phases(
    project_id: int,
) -> list[dict[str, object]]:
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            SELECT
                id,
                position,
                title,
                instruction,
                status,
                source_message_id,
                builder_message_id,
                error,
                created_at,
                updated_at
            FROM build_phases
            WHERE project_id = ?
            ORDER BY position
            """,
            (project_id,),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def replace_build_phases(
    project_id: int,
    phases: list[dict[str, str]],
    *,
    source_message_id: int | None = None,
) -> list[dict[str, object]]:
    """Replace a queued plan unless a Builder phase is already in flight."""
    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        active_cursor = await connection.execute(
            """
            SELECT COUNT(*) AS active_count
            FROM build_phases
            WHERE project_id = ?
              AND status IN ('running', 'awaiting_apply')
            """,
            (project_id,),
        )
        active = await active_cursor.fetchone()
        if active is not None and int(active["active_count"]) > 0:
            await connection.commit()
            return await list_build_phases(project_id)
        await connection.execute(
            "DELETE FROM build_phases WHERE project_id = ?",
            (project_id,),
        )
        for position, phase in enumerate(phases, start=1):
            await connection.execute(
                """
                INSERT INTO build_phases(
                    project_id,
                    position,
                    title,
                    instruction,
                    source_message_id
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    position,
                    phase["title"],
                    phase["instruction"],
                    source_message_id,
                ),
            )
        await connection.execute(
            """
            UPDATE build_projects
            SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (project_id,),
        )
        await connection.commit()
    return await list_build_phases(project_id)


async def claim_next_build_phase(
    project_id: int,
) -> tuple[dict[str, object] | None, bool]:
    """Atomically claim the next phase; bool is true only for a new claim."""
    async with _connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        active_cursor = await connection.execute(
            """
            SELECT *
            FROM build_phases
            WHERE project_id = ?
              AND status IN ('running', 'awaiting_apply')
            ORDER BY position
            LIMIT 1
            """,
            (project_id,),
        )
        active = await active_cursor.fetchone()
        if active is not None:
            await connection.commit()
            return dict(active), False
        pending_cursor = await connection.execute(
            """
            SELECT *
            FROM build_phases
            WHERE project_id = ?
              AND status IN ('blocked', 'pending')
            ORDER BY position
            LIMIT 1
            """,
            (project_id,),
        )
        pending = await pending_cursor.fetchone()
        if pending is None:
            await connection.commit()
            return None, False
        phase_id = int(pending["id"])
        await connection.execute(
            """
            UPDATE build_phases
            SET status = 'running',
                error = NULL,
                builder_message_id = NULL,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (phase_id,),
        )
        result = await connection.execute(
            "SELECT * FROM build_phases WHERE id = ?",
            (phase_id,),
        )
        row = await result.fetchone()
        await connection.commit()
    return (dict(row) if row is not None else None), True


async def set_build_phase_builder_result(
    project_id: int,
    phase_id: int,
    *,
    builder_message_id: int | None,
    status: str,
    error: str | None = None,
) -> dict[str, object] | None:
    if status not in {"awaiting_apply", "blocked"}:
        raise ValueError("Stato risultato fase Build non valido.")
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            UPDATE build_phases
            SET builder_message_id = ?,
                status = ?,
                error = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
              AND project_id = ?
              AND status = 'running'
            """,
            (
                builder_message_id,
                status,
                error,
                phase_id,
                project_id,
            ),
        )
        if cursor.rowcount != 1:
            await connection.rollback()
            return None
        result = await connection.execute(
            "SELECT * FROM build_phases WHERE id = ?",
            (phase_id,),
        )
        row = await result.fetchone()
        await connection.commit()
    return dict(row) if row is not None else None


async def complete_build_phase_for_message(
    project_id: int,
    builder_message_id: int,
) -> dict[str, object] | None:
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            UPDATE build_phases
            SET status = 'completed',
                error = NULL,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE project_id = ?
              AND builder_message_id = ?
              AND status = 'awaiting_apply'
            """,
            (project_id, builder_message_id),
        )
        if cursor.rowcount != 1:
            await connection.rollback()
            return None
        result = await connection.execute(
            """
            SELECT *
            FROM build_phases
            WHERE project_id = ? AND builder_message_id = ?
            """,
            (project_id, builder_message_id),
        )
        row = await result.fetchone()
        await connection.commit()
    return dict(row) if row is not None else None


async def mark_build_message_changes_applied(
    project_id: int,
    message_id: int,
) -> bool:
    async with _connect() as connection:
        cursor = await connection.execute(
            """
            UPDATE build_messages
            SET changes_applied_at = COALESCE(
                changes_applied_at,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            WHERE id = ?
              AND project_id = ?
              AND lane = 'builder'
              AND role = 'assistant'
            """,
            (message_id, project_id),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def record_usage(
    *,
    request_id: str,
    api_key_id: int,
    requested_model: str,
    routed_provider: str,
    resolved_model: str,
    prompt_tokens: int,
    completion_tokens: int,
    status_code: int,
    latency_ms: int,
    error_code: str | None = None,
) -> None:
    total_tokens = prompt_tokens + completion_tokens
    async with _connect() as connection:
        await connection.execute(
            """
            INSERT INTO usage_logs(
                request_id,
                api_key_id,
                requested_model,
                routed_provider,
                resolved_model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                status_code,
                latency_ms,
                error_code
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                api_key_id,
                requested_model,
                routed_provider,
                resolved_model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                status_code,
                latency_ms,
                error_code,
            ),
        )
        await connection.commit()


async def get_usage_dashboard(
    *,
    since_modifier: str | None,
    api_key_id: int | None = None,
    provider: str | None = None,
    limit: int = 50,
) -> dict[str, object]:
    """Return aggregate and recent usage metadata without prompts or secrets."""
    clauses: list[str] = []
    parameters: list[object] = []
    if since_modifier is not None:
        clauses.append(
            "ul.created_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)"
        )
        parameters.append(since_modifier)
    if api_key_id is not None:
        clauses.append("ul.api_key_id = ?")
        parameters.append(api_key_id)
    if provider is not None:
        clauses.append("ul.routed_provider = ?")
        parameters.append(provider)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    api_name_sql = """
        COALESCE(
            ga.name,
            CASE
                WHEN ak.name LIKE 'api:%' THEN substr(ak.name, 5)
                ELSE ak.name
            END,
            'API eliminata'
        )
    """

    async with _connect() as connection:
        summary_cursor = await connection.execute(
            f"""
            SELECT
                COUNT(*) AS request_count,
                COALESCE(SUM(CASE
                    WHEN ul.status_code BETWEEN 200 AND 399 THEN 1 ELSE 0
                END), 0) AS successful_requests,
                COALESCE(SUM(CASE
                    WHEN ul.status_code NOT BETWEEN 200 AND 399 THEN 1 ELSE 0
                END), 0) AS failed_requests,
                COALESCE(SUM(ul.prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(ul.completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(ul.total_tokens), 0) AS total_tokens,
                COALESCE(ROUND(AVG(ul.latency_ms)), 0) AS average_latency_ms
            FROM usage_logs AS ul
            {where_sql}
            """,
            parameters,
        )
        summary_row = await summary_cursor.fetchone()

        api_cursor = await connection.execute(
            f"""
            SELECT
                ul.api_key_id,
                {api_name_sql} AS api_name,
                ga.slug AS api_slug,
                COUNT(*) AS request_count,
                SUM(ul.prompt_tokens) AS prompt_tokens,
                SUM(ul.completion_tokens) AS completion_tokens,
                SUM(ul.total_tokens) AS total_tokens,
                ROUND(AVG(ul.latency_ms)) AS average_latency_ms,
                MAX(ul.created_at) AS last_request_at
            FROM usage_logs AS ul
            LEFT JOIN api_keys AS ak ON ak.id = ul.api_key_id
            LEFT JOIN gateway_apis AS ga ON ga.api_key_id = ul.api_key_id
            {where_sql}
            GROUP BY ul.api_key_id, api_name, ga.slug
            ORDER BY total_tokens DESC, request_count DESC
            """,
            parameters,
        )
        by_api = [dict(row) for row in await api_cursor.fetchall()]

        provider_cursor = await connection.execute(
            f"""
            SELECT
                ul.routed_provider AS provider,
                COUNT(*) AS request_count,
                SUM(ul.prompt_tokens) AS prompt_tokens,
                SUM(ul.completion_tokens) AS completion_tokens,
                SUM(ul.total_tokens) AS total_tokens,
                ROUND(AVG(ul.latency_ms)) AS average_latency_ms
            FROM usage_logs AS ul
            {where_sql}
            GROUP BY ul.routed_provider
            ORDER BY total_tokens DESC, request_count DESC
            """,
            parameters,
        )
        by_provider = [dict(row) for row in await provider_cursor.fetchall()]

        daily_cursor = await connection.execute(
            f"""
            SELECT
                substr(ul.created_at, 1, 10) AS day,
                COUNT(*) AS request_count,
                SUM(ul.prompt_tokens) AS prompt_tokens,
                SUM(ul.completion_tokens) AS completion_tokens,
                SUM(ul.total_tokens) AS total_tokens
            FROM usage_logs AS ul
            {where_sql}
            GROUP BY day
            ORDER BY day
            """,
            parameters,
        )
        daily = [dict(row) for row in await daily_cursor.fetchall()]

        requests_cursor = await connection.execute(
            f"""
            SELECT
                ul.id,
                ul.request_id,
                ul.api_key_id,
                {api_name_sql} AS api_name,
                ga.slug AS api_slug,
                ul.requested_model,
                ul.routed_provider,
                ul.resolved_model,
                ul.prompt_tokens,
                ul.completion_tokens,
                ul.total_tokens,
                ul.status_code,
                ul.latency_ms,
                ul.error_code,
                ul.created_at
            FROM usage_logs AS ul
            LEFT JOIN api_keys AS ak ON ak.id = ul.api_key_id
            LEFT JOIN gateway_apis AS ga ON ga.api_key_id = ul.api_key_id
            {where_sql}
            ORDER BY ul.id DESC
            LIMIT ?
            """,
            [*parameters, max(1, min(limit, 200))],
        )
        requests = [dict(row) for row in await requests_cursor.fetchall()]

        filter_cursor = await connection.execute(
            f"""
            SELECT DISTINCT
                ul.api_key_id,
                {api_name_sql} AS api_name,
                ga.slug AS api_slug
            FROM usage_logs AS ul
            LEFT JOIN api_keys AS ak ON ak.id = ul.api_key_id
            LEFT JOIN gateway_apis AS ga ON ga.api_key_id = ul.api_key_id
            ORDER BY api_name
            """
        )
        api_filters = [dict(row) for row in await filter_cursor.fetchall()]
        provider_filter_cursor = await connection.execute(
            """
            SELECT DISTINCT routed_provider AS provider
            FROM usage_logs
            ORDER BY routed_provider
            """
        )
        provider_filters = [
            str(row["provider"])
            for row in await provider_filter_cursor.fetchall()
        ]

    summary = (
        dict(summary_row)
        if summary_row is not None
        else {
            "request_count": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "average_latency_ms": 0,
        }
    )
    return {
        "summary": summary,
        "by_api": by_api,
        "by_provider": by_provider,
        "daily": daily,
        "requests": requests,
        "filters": {
            "apis": api_filters,
            "providers": provider_filters,
        },
    }
