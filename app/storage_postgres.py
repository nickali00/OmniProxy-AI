"""PostgreSQL adapter for the gateway's bounded internal SQL dialect.

Public requests never supply SQL. Explicit writer transactions use a database
advisory lock to preserve the existing atomic SQLite transaction contract
across processes. This favors correctness over parallel write throughput.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from decimal import Decimal
from functools import lru_cache

import psycopg
from psycopg.pq import TransactionStatus

WRITE_LOCK = 578636754204480001


def validate_transport(url):
    from app.config import settings
    if not settings.require_https:
        return
    try:
        parameters = psycopg.conninfo.conninfo_to_dict(url)
    except psycopg.Error:
        raise sqlite3.OperationalError("Invalid PostgreSQL connection configuration") from None
    hosts = parameters.get("host", "").split(",")
    local = all(not host or host.startswith("/") or host in {"postgres", "localhost", "127.0.0.1", "::1"} for host in hosts)
    if (not local or parameters.get("service")) and parameters.get("sslmode") != "verify-full":
        raise sqlite3.OperationalError("Remote PostgreSQL requires sslmode=verify-full in the secure profile")
SERIAL = {"api_keys": "id", "gateway_apis": "id", "skills": "id", "mcp_servers": "id", "usage_logs": "id", "mcp_tool_usage": "id", "build_projects": "id", "build_project_files": "id", "build_artifacts": "id", "build_messages": "id", "build_phases": "id", "agent_job_events": "seq", "administration_audit": "seq"}

COMPATIBILITY_SQL = """
CREATE COLLATION IF NOT EXISTS omniproxy_nocase
 (provider=icu,locale='und-u-ks-level2',deterministic=false);
CREATE OR REPLACE FUNCTION omni_strftime(pattern text, stamp text, modifier text DEFAULT NULL)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE moment timestamptz;
BEGIN
 moment := CASE WHEN stamp='now' THEN clock_timestamp() ELSE stamp::timestamptz END;
 IF modifier IS NOT NULL THEN moment := moment + modifier::interval; END IF;
 IF pattern='%s' THEN RETURN extract(epoch FROM moment)::text; END IF;
 IF pattern='%Y-%m-%dT%H:%M:%fZ' THEN
  RETURN to_char(moment AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.MS"Z"');
 END IF;
 RAISE EXCEPTION 'Unsupported internal timestamp format';
END; $$;
"""


class Row(Mapping):
    def __init__(self, names, values):
        self.names = names
        # PostgreSQL SUM(bigint)/AVG return NUMERIC. Preserve exact integer
        # counts and SQLite-compatible JSON numbers for fractional aggregates.
        self.values = tuple(
            (int(value) if value == value.to_integral_value() else float(value)) if isinstance(value, Decimal) else value
            for value in values
        )

    def __getitem__(self, key):
        return self.values[key if isinstance(key, int) else self.names.index(key)]

    def __iter__(self):
        return iter(self.names)

    def __len__(self):
        return len(self.values)


@lru_cache(maxsize=512)
def translate(statement):
    sql = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", statement, flags=re.IGNORECASE)
    sql = re.sub(r"\bINTEGER\b", "BIGINT", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bBLOB\b", "BYTEA", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bCOLLATE\s+NOCASE\b", 'COLLATE "omniproxy_nocase"', sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bstrftime\s*\(", "omni_strftime(", sql, flags=re.IGNORECASE)
    # Bind markers are translated only outside literals and comments. Percent
    # characters are escaped for psycopg's parameter binding even in literals.
    result, quote, comment, index = [], None, None, 0
    while index < len(sql):
        char = sql[index]
        pair = sql[index:index + 2]
        if comment == "line":
            if char == "\n":
                comment = None
        elif comment == "block":
            if pair == "*/":
                result.append(pair)
                index += 2
                comment = None
                continue
        elif quote:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    result.append(char * 2)
                    index += 2
                    continue
                quote = None
        elif pair in {"--", "/*"}:
            comment = "line" if pair == "--" else "block"
        elif char in {"'", '"'}:
            quote = char
        elif char == "?":
            result.append("%s")
            index += 1
            continue
        result.append("%%" if char == "%" else char)
        index += 1
    return "".join(result)


class Cursor:
    def __init__(self, cursor, lastrowid=None):
        self.cursor, self.lastrowid = cursor, lastrowid
        self.rowcount = cursor.rowcount

    async def fetchone(self):
        values = await self.cursor.fetchone()
        return Row(tuple(column.name for column in self.cursor.description), values) if values is not None else None

    async def fetchall(self):
        values = await self.cursor.fetchall()
        names = tuple(column.name for column in self.cursor.description)
        return [Row(names, row) for row in values]


class Connection:
    def __init__(self, connection):
        self.connection = connection

    async def execute(self, statement, parameters=()):
        try:
            if statement.strip().upper() == "BEGIN IMMEDIATE":
                if self.connection.info.transaction_status == TransactionStatus.IDLE:
                    await self.connection.execute("BEGIN")
                cursor = await self.connection.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
                return Cursor(cursor)
            sql = translate(statement).strip().rstrip(";")
            insert = re.match(r"INSERT\s+INTO\s+(\w+)\b", sql, re.IGNORECASE)
            returning = insert and insert[1] in SERIAL and not re.search(r"\bRETURNING\b", sql, re.IGNORECASE)
            if returning:
                sql += " RETURNING " + SERIAL[insert[1]]
            cursor = await self.connection.execute(sql, parameters)
            lastrowid = None
            if returning:
                row = await cursor.fetchone()
                lastrowid = row[0] if row else None
            return Cursor(cursor, lastrowid)
        except psycopg.IntegrityError:
            raise sqlite3.IntegrityError("PostgreSQL constraint violation") from None
        except psycopg.Error as exc:
            raise sqlite3.OperationalError(f"PostgreSQL {type(exc).__name__} ({exc.sqlstate})") from None

    async def executemany(self, statement, parameters):
        # Preserve row-by-row exceptions/transaction behavior. Bounded batches
        # in this gateway are small; this avoids implicit pipeline semantics.
        cursor = None
        for values in parameters:
            cursor = await self.execute(statement, values)
        return cursor

    async def executescript(self, script):
        await self.execute("BEGIN IMMEDIATE")
        # Internal schema scripts contain no procedural bodies or ';' strings.
        for statement in script.split(";"):
            if statement.strip():
                await self.execute(statement)
        await self.commit()

    async def commit(self):
        await self.connection.commit()

    async def rollback(self):
        await self.connection.rollback()

    async def close(self):
        await self.connection.close()


async def connect(url):
    validate_transport(url)
    try:
        connection = await psycopg.AsyncConnection.connect(url, connect_timeout=5, options="-c timezone=UTC -c lock_timeout=5000 -c statement_timeout=30000 -c idle_in_transaction_session_timeout=60000")
    except psycopg.Error:
        raise sqlite3.OperationalError("PostgreSQL connection unavailable") from None
    return Connection(connection)


async def init_core(connection, schema):
    await connection.execute("BEGIN IMMEDIATE")
    # No bind parameters in the fixed function body; '%' belongs to patterns.
    await connection.connection.execute(COMPATIBILITY_SQL)
    await connection.executescript(schema)
