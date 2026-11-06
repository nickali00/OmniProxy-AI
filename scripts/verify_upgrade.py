"""Rehearse additive SQLite migrations on a temporary copy, never the source."""

import argparse
import asyncio
import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from app.citations import init_citations
from app.config import settings
from app.database import init_database
from app.file_uploads import init_files
from app.job_store import init_jobs
from app.job_tests import init_tests
from app.shared_capacity import init_capacity
from scripts.backup_sqlite import copy_database


def quoted(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def inventory(path, columns=None):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Integrity check failed")
        if columns is None:
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='schema_versions'")]
            columns = {table: [row[1] for row in db.execute(f"PRAGMA table_info({quoted(table)})")] for table in tables}
        result = {}
        for table, names in columns.items():
            rows = db.execute("SELECT " + ",".join(quoted(name) for name in names) + " FROM " + quoted(table))
            hashes = sorted(hashlib.sha256(json.dumps(list(row), default=lambda value: {"bytes": value.hex()}, sort_keys=True).encode()).hexdigest() for row in rows)
            result[table] = hashlib.sha256(json.dumps(hashes).encode()).hexdigest()
        return columns, result


async def verify(source):
    with tempfile.TemporaryDirectory(prefix="omniproxy-upgrade-") as temporary:
        settings.database_url = ""
        settings.admin_auth_required = False
        settings.dashboard_auth_required = False
        settings.require_https = False
        settings.otel_enabled = False
        settings.database_path = Path(temporary) / "upgrade.sqlite3"
        copy_database(source, settings.database_path)
        columns, original = inventory(settings.database_path)
        for _ in range(2):
            await init_database()
            await init_jobs()
            await init_tests()
            await init_files()
            await init_capacity()
            await init_citations()
            _, current = inventory(settings.database_path, columns)
            if current != original:
                raise ValueError("An existing row changed during migration")
        print(json.dumps({"scope": "temporary_copy_of_read_only_backup", "migration_passes": 2, "old_tables_verified_by_row_hash": len(columns), "integrity_and_foreign_keys": True, "passed": True}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    asyncio.run(verify(parser.parse_args().source.resolve(strict=True)))
