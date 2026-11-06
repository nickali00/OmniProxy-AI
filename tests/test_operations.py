import sqlite3

import pytest

from scripts.backup_sqlite import copy_database
from scripts.prepare_organization import prepare


def test_sqlite_online_wal_backup_restore_and_no_overwrite(tmp_path):
    source, snapshot, restored = (tmp_path / name for name in ("source.sqlite3", "snapshot.sqlite3", "restored.sqlite3"))
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE fixture(id INTEGER PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO fixture VALUES (1,'BLUE')")
        db.commit()
        assert copy_database(source, snapshot)["integrity_verified"]
        db.execute("UPDATE fixture SET value='RED'")
        db.commit()
    assert snapshot.stat().st_mode & 0o777 == 0o600
    copy_database(snapshot, restored)
    with sqlite3.connect(restored) as db:
        assert db.execute("SELECT value FROM fixture").fetchone()[0] == "BLUE"
    with pytest.raises(FileExistsError):
        copy_database(source, snapshot)


def test_organization_preparation_never_overwrites_secrets(tmp_path):
    directory = tmp_path / "private"
    path = prepare(directory)
    assert directory.stat().st_mode & 0o777 == 0o700 and path.stat().st_mode & 0o777 == 0o600
    original = path.read_bytes()
    assert b"omniproxy_runtime" in original
    with pytest.raises(FileExistsError):
        prepare(directory)
    assert path.read_bytes() == original
