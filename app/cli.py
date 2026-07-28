from __future__ import annotations

import argparse
import asyncio
import json

from app.database import (
    create_api_key,
    init_database,
    list_api_keys,
    revoke_api_key,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage local gateway API keys.")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-key", help="Create a new API key.")
    create.add_argument("--name", required=True)

    commands.add_parser("list-keys", help="List key metadata (never secrets).")

    revoke = commands.add_parser("revoke-key", help="Revoke a key by numeric ID.")
    revoke.add_argument("--id", type=int, required=True)
    return parser


async def _run(args: argparse.Namespace) -> int:
    await init_database()

    if args.command == "create-key":
        record, secret = await create_api_key(args.name)
        print(f"id={record.id} name={record.name} key={secret}")
        print("The secret is shown once; store it now.")
        return 0

    if args.command == "list-keys":
        print(json.dumps(await list_api_keys(), indent=2))
        return 0

    if args.command == "revoke-key":
        revoked = await revoke_api_key(args.id)
        print("revoked" if revoked else "not found or already revoked")
        return 0 if revoked else 1

    return 2


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))


if __name__ == "__main__":
    main()
