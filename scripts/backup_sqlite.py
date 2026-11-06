"""Consistent backup or restore into a NEW private SQLite file."""

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path


def copy_database(source, output):
    source, output = Path(source).resolve(strict=True), Path(output)
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(output)) as target:
            original.backup(target, pages=128, sleep=0.05)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or target.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Database integrity verification failed")
            target.execute("PRAGMA journal_mode=DELETE")
        with output.open("rb") as snapshot:
            checksum = hashlib.file_digest(snapshot, "sha256").hexdigest()
        return {"file": str(output.resolve()), "sha256": checksum, "integrity_verified": True}
    except BaseException:
        output.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "restore"])
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(copy_database(args.source, args.output)))
