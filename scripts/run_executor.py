"""Operator-run pull executor. Starts no listener and stores no API keys.

OMNIPROXY_API_KEY must contain a key with explicit workspace/test grants.
Example: python -m scripts.run_executor --url http://127.0.0.1:8181 --once
Supports Linux and Docker Desktop's Linux engine; native Windows is untested.
"""

import argparse
import json
import os
import time
from urllib.parse import urlsplit

import httpx

from app.test_sandbox import PROFILES, run_snapshot


def run_once(client):
    claimed = client.post("/v1/job-tests/claim", json={"profiles": list(PROFILES)})
    claimed.raise_for_status()
    if claimed.status_code == 204:
        return None
    claim = claimed.json()
    lease = {"lease_token": claim["lease_token"]}
    last_heartbeat, cancelled = 0.0, False

    def check_cancel():
        nonlocal last_heartbeat, cancelled
        if time.monotonic() - last_heartbeat >= min(5, claim["lease_seconds"] / 3):
            response = client.post(f"/v1/job-tests/{claim['id']}/heartbeat", json=lease)
            response.raise_for_status()
            cancelled = response.json()["cancel_requested"]
            last_heartbeat = time.monotonic()
        return cancelled

    check_cancel()
    if cancelled:
        result = {"state": "cancelled", "exit_code": None, "output": "", "output_truncated": False, "error_code": None}
    else:
        result = run_snapshot(claim["profile"], claim["files"], timeout=min(300, claim["timeout_seconds"]), should_cancel=check_cancel)
    response = client.post(f"/v1/job-tests/{claim['id']}/result", json={**lease, **result})
    response.raise_for_status()
    return {"id": claim["id"], "state": result["state"], "exit_code": result["exit_code"], "output_truncated": result["output_truncated"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    parts = urlsplit(args.url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}:
        parser.error("URL gateway non valido.")
    secret = os.environ.get("OMNIPROXY_API_KEY", "").strip()
    if not secret:
        parser.error("Imposta OMNIPROXY_API_KEY nell'ambiente; non usare chiavi negli argomenti.")
    with httpx.Client(base_url=args.url, headers={"Authorization": f"Bearer {secret}"}, timeout=10, follow_redirects=False) as client:
        try:
            while True:
                result = run_once(client)
                if result:
                    print(json.dumps(result), flush=True)
                if args.once:
                    return
                time.sleep(1)
        except httpx.HTTPError:
            raise SystemExit("Connessione esecutore interrotta; il lavoro non viene rieseguito automaticamente.") from None


if __name__ == "__main__":
    main()
