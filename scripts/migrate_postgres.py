"""Reviewed, offline SQLite snapshot -> empty PostgreSQL database migration.

Target DSN comes only from OMNIPROXY_TARGET_DATABASE_URL. Source is never
modified. Existing target data is never overwritten or merged.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.citations import init_citations
from app.config import settings
from app.database import _connect, init_database
from app.file_uploads import init_files
from app.job_store import init_jobs
from app.job_tests import init_tests
from app.shared_capacity import init_capacity
from app.storage_postgres import SERIAL


def identifier(value):
    if not re.fullmatch(r"[a-z_][a-z_0-9]*", value):
        raise ValueError("Unsupported snapshot identifier")
    return '"' + value + '"'


@contextmanager
def source_database(path):
    path = Path(path).resolve(strict=True)
    if Path(str(path) + "-wal").exists():
        raise ValueError("Use a standalone SQLite backup, not a live WAL database")
    db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
        db.close()
        raise ValueError("Snapshot integrity check failed")
    try:
        yield db
    finally:
        db.close()


def tables(db):
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    dependencies = {name: {row[2] for row in db.execute(f"PRAGMA foreign_key_list({identifier(name)})") if row[2] != name} & names for name in names}
    result = []
    while dependencies:
        ready = sorted(name for name, deps in dependencies.items() if not deps)
        if not ready:
            raise ValueError("Unsupported cyclic snapshot dependencies")
        result.extend(ready)
        dependencies = {name: deps - set(ready) for name, deps in dependencies.items() if name not in ready}
    return result


def row_digest(rows):
    checksum = hashlib.sha256()
    for row in rows:
        values = [{"bytes": base64.b64encode(bytes(value)).decode()} if isinstance(value, (bytes, memoryview)) else value for value in row]
        checksum.update(json.dumps(values, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode())
        checksum.update(b"\n")
    return checksum.hexdigest()


def file_digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


async def migrate(source, approval_hash):
    fingerprint = file_digest(source)
    if approval_hash != fingerprint:
        raise ValueError("Snapshot changed or approval hash does not match")
    with source_database(source) as old:
        order = [table for table in tables(old) if table != "schema_versions"]
        await init_database()
        await init_jobs()
        await init_tests()
        await init_files()
        await init_capacity()
        await init_citations()
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            target = await (await db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE'")).fetchall()
            target_names = {row[0] for row in target}
            if not set(order) <= target_names:
                raise ValueError("Snapshot contains unsupported tables")
            for table in target_names - {"schema_versions"}:
                count = await (await db.execute(f"SELECT count(*) FROM {identifier(table)}")).fetchone()
                if count[0]:
                    raise ValueError("Target database must be empty")
            counts = {}
            for table in order:
                columns = [row[1] for row in old.execute(f"PRAGMA table_info({identifier(table)})")]
                names = ",".join(identifier(column) for column in columns)
                rows = old.execute(f"SELECT {names} FROM {identifier(table)} ORDER BY {names}")
                count = 0
                while batch := rows.fetchmany(100):
                    await db.executemany(f"INSERT INTO {identifier(table)} ({names}) VALUES ({','.join('?' for _ in columns)})", batch)
                    count += len(batch)
                original = old.execute(f"SELECT {names} FROM {identifier(table)} ORDER BY {names}")
                expected_hash = row_digest(original)
                checksum, verified_count = hashlib.sha256(), 0
                async with db.connection.cursor(name="verify_snapshot") as cursor:
                    await cursor.execute(f"SELECT {names} FROM {identifier(table)} ORDER BY {names}")
                    async for row in cursor:
                        values = [{"bytes": base64.b64encode(bytes(value)).decode()} if isinstance(value, (bytes, memoryview)) else value for value in row]
                        checksum.update(json.dumps(values, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode())
                        checksum.update(b"\n")
                        verified_count += 1
                if count != verified_count or expected_hash != checksum.hexdigest():
                    raise ValueError("Copied snapshot verification failed")
                counts[table] = count
            for table, column in SERIAL.items():
                await db.execute(f"SELECT setval(pg_get_serial_sequence(?,?),COALESCE((SELECT MAX({identifier(column)}) FROM {identifier(table)}),1),EXISTS(SELECT 1 FROM {identifier(table)}))", (table, column))
            if file_digest(source) != fingerprint:
                raise ValueError("Source snapshot changed during migration")
            await db.commit()
    return {"source_sha256": fingerprint, "rows": counts, "verified": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--approve-sha256")
    args = parser.parse_args()
    if not args.approve_sha256:
        with source_database(args.source) as db:
            print(json.dumps({"sha256": file_digest(args.source), "rows": {name: db.execute(f"SELECT count(*) FROM {identifier(name)}").fetchone()[0] for name in tables(db)}}))
        return
    settings.database_url = os.environ.get("OMNIPROXY_TARGET_DATABASE_URL", "")
    if not settings.database_url:
        raise ValueError("OMNIPROXY_TARGET_DATABASE_URL is required")
    print(json.dumps(asyncio.run(migrate(args.source, args.approve_sha256))))


if __name__ == "__main__":
    main()
