"""Real Codex proposal → local Docker tests → approval → applied tests.

All sources, keys and DB are temporary. No listener or Docker socket is
added to the gateway. Invoke the same pull CLI library an operator uses.
"""

import argparse
import json
import logging
import tempfile
import time
from functools import partial
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.database import create_gateway_api
from app.main import app
from app.test_sandbox import run_snapshot
from scripts.run_executor import run_once

ADMIN = {"Origin": "http://127.0.0.1", "X-OmniProxy-Request": "dashboard"}
TESTS = """import os,socket,unittest
from pathlib import Path
from a import add
class Checks(unittest.TestCase):
    def test_addition(self): self.assertEqual(add(2,3),5)
    def test_isolation(self):
        self.assertNotEqual(os.getuid(),0)
        self.assertFalse(Path('/var/run/docker.sock').exists())
        with self.assertRaises(OSError): Path('/forbidden').write_text('x')
        with socket.socket() as connection:
            connection.settimeout(0.2)
            self.assertNotEqual(connection.connect_ex(('1.1.1.1',443)),0)
"""


def verify(broker):
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="omniproxy-job-tests-") as temporary:
        root = Path(temporary) / "repo"
        root.mkdir()
        original = "def add(a,b):\n    return a-b\n"
        (root / "a.py").write_text(original)
        (root / "test_a.py").write_text(TESTS)
        settings.database_url = ""
        settings.admin_auth_required = False
        settings.dashboard_auth_required = False
        settings.require_https = False
        settings.otel_enabled = False
        settings.database_path = Path(temporary) / "gateway.sqlite3"
        settings.provider_vault_key_path = Path(temporary) / "vault.key"
        settings.bootstrap_api_key = "sk-local-job-tests-key-with-thirty-two-characters"
        settings.job_workspace_roots = str(root)
        settings.codex_broker_url = broker
        with TestClient(app, base_url="http://127.0.0.1") as client:
            profile, key = client.portal.call(partial(create_gateway_api, name="Tests E2E", provider="codex", model="gpt-5.6-sol", reasoning_effort="low"))
            client.headers.update({"Authorization": f"Bearer {key}"})
            workspace = client.post("/api/workspaces", headers=ADMIN, json={"name": "Synthetic repo", "root": str(root), "paths": ["a.py", "test_a.py"], "api_key_ids": [profile.api_key_id], "writable": True, "test_profiles": ["python-unittest"]})
            assert workspace.status_code == 201, workspace.text
            created = client.post("/v1/jobs", headers={"Idempotency-Key": "proposal"}, json={"workspace_id": workspace.json()["id"], "instruction": "Fix add in a.py so it adds both parameters. Change only a.py. Preserve test_a.py exactly; tests are trusted acceptance criteria."})
            assert created.status_code == 202
            job_id = created.json()["id"]
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                job = client.get(f"/v1/jobs/{job_id}").json()
                if job["state"] == "awaiting_approval":
                    break
                assert job["state"] not in {"failed", "cancelled"}, job.get("error_code")
                time.sleep(0.1)
            assert job["state"] == "awaiting_approval"
            assert {change["path"] for change in job["proposal"]["changes"]} == {"a.py"}
            for target in ("proposal", "applied"):
                if target == "applied":
                    approved = client.post(f"/v1/jobs/{job_id}/approvals", json={"decision": "approve", "proposal_hash": job["proposal"]["hash"]})
                    assert approved.status_code == 200
                queued = client.post(f"/v1/jobs/{job_id}/tests", headers={"Idempotency-Key": target}, json={"profile": "python-unittest", "proposal_hash": job["proposal"]["hash"], "target": target})
                assert queued.status_code == 202, queued.text
                result = run_once(client)
                assert result["state"] == "completed" and result["exit_code"] == 0, result
                repeated = client.post(f"/v1/jobs/{job_id}/tests", headers={"Idempotency-Key": target}, json={"profile": "python-unittest", "proposal_hash": job["proposal"]["hash"], "target": target})
                assert repeated.status_code == 200 and run_once(client) is None
                if target == "proposal":
                    assert (root / "a.py").read_text() == original
            assert (root / "test_a.py").read_text() == TESTS
            assert len(client.get(f"/v1/jobs/{job_id}/tests").json()["data"]) == 2
    timed = run_snapshot("python-unittest", {"test_sleep.py": "import time\ntime.sleep(10)\n"}, timeout=0.5)
    assert timed["state"] == "timed_out", timed
    flooded = run_snapshot("python-unittest", {"test_output.py": "import unittest\nprint('X'*100000)\nclass One(unittest.TestCase):\n    def test_ok(self): pass\n"})
    assert flooded["state"] == "completed" and flooded["output_truncated"] and len(flooded["output"].encode()) <= 65536
    cancelled = run_snapshot("python-unittest", {"test_wait.py": "import time\ntime.sleep(10)\n"}, should_cancel=lambda: True)
    assert cancelled["state"] == "cancelled"
    print(json.dumps({"scope": "temporary_gateway_and_local_cli_docker", "real_codex_proposal": True, "tests_before_and_after_approval": True, "snapshot_only": True, "isolation_checks": True, "timeout_cancel_output_cap": True, "passed": True}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker-url", required=True)
    verify(parser.parse_args().broker_url)
