"""Explicit local extension of one active operator token; prints metadata only."""

import argparse
import asyncio
import json
from pathlib import Path

from app.config import settings
from app.identity import extend_operator_session


async def run(args):
    if args.database:
        settings.database_url = ""
        settings.database_path = args.database.resolve(strict=True)
    elif not settings.database_url:
        raise ValueError("Configure DATABASE_URL before PostgreSQL operation")
    path = args.credential_file
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Use a private operator credential file (0600)")
    value = json.loads(path.read_text())
    result = await extend_operator_session(value["session_id"], value["token"], years=args.years)
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    storage = parser.add_mutually_exclusive_group(required=True)
    storage.add_argument("--database", type=Path)
    storage.add_argument("--postgres", action="store_true")
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--years", required=True, type=int, choices=range(1, 6))
    asyncio.run(run(parser.parse_args()))
