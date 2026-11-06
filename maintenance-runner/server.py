from __future__ import annotations

import hmac
import json
import os
import re
import subprocess
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "0.0.0.0"
PORT = 8790
WORKSPACE = "/workspace"
CODEX_VERSION_FILE = "/workspace/codex-broker/version.txt"
MAX_BODY_BYTES = 16_384
MAX_OUTPUT_CHARACTERS = 16_000

# The LLM never supplies argv. It can only select one of these fixed actions.
COMMANDS = {
    "docker compose ps": ["docker", "compose", "ps"],
    "docker compose up -d --build --force-recreate": [
        "docker",
        "compose",
        "up",
        "-d",
        "--build",
        "--force-recreate",
        "--no-deps",
        "--wait",
        "--wait-timeout",
        "180",
        "gateway",
        "codex-broker",
        "claude-broker",
        "antigravity-broker",
    ],
}

jobs: dict[str, dict[str, object]] = {}
jobs_lock = threading.Lock()
execution_lock = threading.Lock()
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$")


def public_job(job_id: str) -> dict[str, object] | None:
    with jobs_lock:
        job = jobs.get(job_id)
        return dict(job) if job is not None else None


def run_job(job_id: str, requested: list[str]) -> None:
    # Give the HTTP response enough time to reach the gateway before a rebuild
    # recreates that container.
    time.sleep(1.0)
    with execution_lock:
        with jobs_lock:
            jobs[job_id]["status"] = "running"
            jobs[job_id]["started_at"] = time.time()
        output: list[str] = []
        status = "completed"
        return_code = 0
        for command in requested:
            try:
                result = subprocess.run(
                    COMMANDS[command],
                    cwd=WORKSPACE,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=1_200,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                status = "failed"
                return_code = -1
                output.append(f"$ {command}\n{type(exc).__name__}")
                break
            return_code = result.returncode
            output.append(f"$ {command}\n{result.stdout}\n{result.stderr}")
            if result.returncode != 0:
                status = "failed"
                break
        rendered_output = "\n".join(output)[-MAX_OUTPUT_CHARACTERS:]
        with jobs_lock:
            jobs[job_id].update(
                {
                    "status": status,
                    "return_code": return_code,
                    "output": rendered_output,
                    "finished_at": time.time(),
                }
            )


def run_codex_update(job_id: str, version: str) -> None:
    time.sleep(1.0)
    with execution_lock:
        with jobs_lock:
            jobs[job_id].update(status="running", started_at=time.time())
        output = []
        status = "completed"
        code = 0
        for args in (
            ["docker", "compose", "build", "--build-arg", f"CODEX_CLI_VERSION={version}", "codex-broker"],
            ["docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "180", "codex-broker"],
            ["docker", "compose", "exec", "-T", "codex-broker", "codex", "--version"],
        ):
            try:
                result = subprocess.run(args, cwd=WORKSPACE, capture_output=True, text=True, timeout=1_200, check=False)
                code = result.returncode
                output.append(f"$ {' '.join(args[:3])}\n{result.stdout}\n{result.stderr}")
                if code != 0 or (args[2] == "exec" and result.stdout.strip() != f"codex-cli {version}"):
                    status = "failed"
                    break
            except (OSError, subprocess.TimeoutExpired) as exc:
                status, code = "failed", -1
                output.append(type(exc).__name__)
                break
        if status == "completed":
            try:
                with open(CODEX_VERSION_FILE, "r+", encoding="utf-8") as handle:
                    original = handle.read().strip()
                    if not VERSION_RE.fullmatch(original):
                        raise ValueError("codex_version_pin_invalid")
                    handle.seek(0)
                    handle.write(f"{version}\n")
                    handle.truncate()
            except (OSError, ValueError) as exc:
                status, code = "failed", -1
                output.append(type(exc).__name__)
        with jobs_lock:
            jobs[job_id].update(status=status, return_code=code,
                output="\n".join(output)[-MAX_OUTPUT_CHARACTERS:], finished_at=time.time())


class Handler(BaseHTTPRequestHandler):
    server_version = "OmniProxyMaintenance/1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def authorized(self) -> bool:
        try:
            configured = os.environ.get("MAINTENANCE_TOKEN_PATH", "")
            token = Path(configured).read_text().strip() if configured else ""
            if not 43 <= len(token) <= 128:
                raise ValueError()
        except (OSError, ValueError):
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "control_auth_not_configured"})
            return False
        value = self.headers.get("Authorization", "")
        if not hmac.compare_digest(value.encode(), ("Bearer " + token).encode()):
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_control_credential"})
            return False
        return True

    def send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, object],
    ) -> None:
        content = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if not self.authorized():
            return
        if self.path.startswith("/v1/jobs/"):
            job_id = self.path.removeprefix("/v1/jobs/")
            job = public_job(job_id)
            if job is None:
                self.send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "job_not_found"},
                )
                return
            self.send_json(HTTPStatus.OK, job)
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self.authorized():
            return
        with jobs_lock:
            active = sum(job["status"] in {"queued", "running"} for job in jobs.values())
            if len(jobs) >= 1000:
                for identifier in list(jobs):
                    if jobs[identifier]["status"] in {"completed", "failed"}:
                        del jobs[identifier]
                        if len(jobs) < 1000:
                            break
        if active >= 2 or len(jobs) >= 1000:
            self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "control_busy"})
            return
        if self.path == "/v1/codex/update":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 200:
                    raise ValueError()
                version = json.loads(self.rfile.read(length))["version"]
                if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
                    raise ValueError()
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_version"})
                return
            job_id = str(uuid.uuid4())
            with jobs_lock:
                jobs[job_id] = {"id": job_id, "status": "queued", "version": version, "created_at": time.time()}
            threading.Thread(target=run_codex_update, args=(job_id, version), daemon=True).start()
            self.send_json(HTTPStatus.ACCEPTED, dict(jobs[job_id]))
            return
        if self.path != "/v1/commands":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_request"},
            )
            return
        try:
            payload = json.loads(self.rfile.read(length))
            requested = payload["commands"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_request"},
            )
            return
        if (
            not isinstance(requested, list)
            or not requested
            or len(requested) > 2
            or any(command not in COMMANDS for command in requested)
        ):
            self.send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "command_not_allowed"},
            )
            return
        job_id = str(uuid.uuid4())
        with jobs_lock:
            jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "commands": requested,
                "created_at": time.time(),
            }
        thread = threading.Thread(
            target=run_job,
            args=(job_id, requested),
            daemon=True,
        )
        thread.start()
        self.send_json(HTTPStatus.ACCEPTED, dict(jobs[job_id]))


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
