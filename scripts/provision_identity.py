"""Operator-local provisioning. The token is written only to a new 0600 file."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from app.config import settings
from app.database import init_database
from app.identity import provision, renew_session


async def run(args):
    if args.database:
        settings.database_url = ""
        settings.database_path = Path(args.database)
    elif not settings.database_url:
        raise ValueError("Configure DATABASE_URL before PostgreSQL provisioning")
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        await init_database()
        if args.identity_id:
            if args.operator or args.tenant:
                raise ValueError("Renewal does not change identity permissions")
            value = await renew_session(args.identity_id, hours=args.hours)
        else:
            value = await provision(args.name, operator=args.operator, tenant=args.tenant, role=args.role, hours=args.hours)
        with os.fdopen(descriptor, "w") as output:
            descriptor = None
            json.dump(value, output)
            output.write("\n")
        print(json.dumps({"identity_id": value["identity_id"], "session_id": value["session_id"], "credential_file": str(Path(args.output).resolve())}))
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        Path(args.output).unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    storage = parser.add_mutually_exclusive_group(required=True)
    storage.add_argument("--database")
    storage.add_argument("--postgres", action="store_true")
    subject = parser.add_mutually_exclusive_group(required=True)
    subject.add_argument("--name")
    subject.add_argument("--identity-id")
    parser.add_argument("--output", required=True)
    parser.add_argument("--operator", action="store_true")
    parser.add_argument("--tenant")
    parser.add_argument("--role", choices=["owner", "admin", "developer", "viewer"], default="viewer")
    parser.add_argument("--hours", type=int, default=24)
    asyncio.run(run(parser.parse_args()))
