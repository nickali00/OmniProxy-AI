"""Prepare a NEW private env file; never alter a running deployment's secrets."""

import argparse
import json
import os
import secrets
from pathlib import Path

from app.config_bundles import write_private


def prepare(directory):
    directory = Path(directory)
    os.mkdir(directory, 0o700)
    administrator, application = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
    path = directory / "organization.env"
    write_private(path, f"COMPOSE_PROJECT_NAME=omniproxy-organization\nAI_GATEWAY_NETWORK=omniproxy-organization-network\nORG_POSTGRES_PASSWORD={administrator}\nORG_APP_PASSWORD={application}\nORG_DATABASE_URL=postgresql://omniproxy_runtime:{application}@postgres:5432/omniproxy\n")
    return path.resolve()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    print(json.dumps({"private_env_file": str(prepare(parser.parse_args().directory))}))
