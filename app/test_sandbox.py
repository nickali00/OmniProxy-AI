"""Local CLI-only test execution. No HTTP listener or gateway Docker access."""

from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from pathlib import PurePosixPath

PROFILES = {
    "python-unittest": ["-I", "-m", "unittest", "discover", "-s", ".", "-p", "test*.py", "-v"],
    "python-compile": ["-I", "-c", "import compileall,sys; sys.exit(0 if compileall.compile_dir('/workspace',quiet=1,force=True) else 1)"],
}
MAX_OUTPUT = 65_536
PYTHON_IMAGE = "python:3.12-slim@sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e"
BOOTSTRAP = """
import json,os,pathlib,sys
data=json.load(sys.stdin)
root=pathlib.Path('/workspace')
for name,content in data['files'].items():
    path=root/name
    if not path.resolve().is_relative_to(root): raise ValueError('invalid path')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(content,encoding='utf-8')
os.execv(sys.executable,[sys.executable]+data['command'])
"""


def validate_files(files):
    if not isinstance(files, dict) or not 1 <= len(files) <= 50:
        raise ValueError("invalid_snapshot")
    total = 0
    for name, content in files.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise TypeError("invalid_snapshot")
        path = PurePosixPath(name)
        if not name or len(name) > 512 or path.is_absolute() or str(path) != name or "\\" in name or any(ord(char) < 32 for char in name) or "\x00" in content:
            raise ValueError("invalid_snapshot")
        if any(part in {"..", ".git", ".ssh", ".aws", ".codex"} or part.lower().startswith(".env") or part.lower().endswith((".key", ".pem", ".p12", ".pfx")) or part.lower() in {"auth.json", "credentials", "credentials.json", "id_rsa", "id_ed25519"} for part in path.parts):
            raise ValueError("invalid_snapshot")
        size = len(content.encode())
        total += size
        if size > 256_000 or total > 1_000_000:
            raise ValueError("snapshot_too_large")


def run_snapshot(profile, files, *, timeout=30, should_cancel=lambda: False):
    """Run only a fixed profile against copies on tmpfs; return bounded output."""
    validate_files(files)
    if profile not in PROFILES or not 0 < timeout <= 300:
        raise ValueError("test_profile_not_allowed")
    container_id = None
    process = writer = reader = None
    output = bytearray()
    truncated = False
    state, code, error = "failed", None, None
    try:
        arguments = ["docker", "create", "--pull", "never", "-i", "--name", "omniproxy-test-" + uuid.uuid4().hex,
            "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "64", "--memory", "256m", "--memory-swap", "256m", "--cpus", "1",
            "--user", "65534:65534", "--workdir", "/workspace", "--log-driver", "none",
            "--tmpfs", "/workspace:rw,noexec,nosuid,nodev,size=32m,mode=1777",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777",
            "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            PYTHON_IMAGE, "python", "-I", "-c", BOOTSTRAP]
        created = subprocess.run(arguments, capture_output=True, timeout=20, check=True)
        container_id = created.stdout.decode().strip()
        if len(container_id) != 64 or any(character not in "0123456789abcdef" for character in container_id):
            container_id = None
            raise ValueError("invalid_container_id")
        process = subprocess.Popen(["docker", "start", "-ai", container_id], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        encoded = json.dumps({"files": files, "command": PROFILES[profile]}).encode()

        def feed():
            try:
                process.stdin.write(encoded)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        def drain():
            nonlocal truncated
            while chunk := process.stdout.read(4096):
                remaining = MAX_OUTPUT - len(output)
                output.extend(chunk[:remaining])
                truncated |= len(chunk) > remaining

        writer = threading.Thread(target=feed, daemon=True)
        reader = threading.Thread(target=drain, daemon=True)
        writer.start()
        reader.start()
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if should_cancel() or time.monotonic() >= deadline:
                state = "cancelled" if time.monotonic() < deadline else "timed_out"
                subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, timeout=10, check=False)
                break
            time.sleep(0.05)
        code = process.wait(timeout=10)
        if state not in {"cancelled", "timed_out"}:
            state = "completed" if code == 0 else "failed"
    except (OSError, ValueError, subprocess.SubprocessError):
        error = "test_executor_unavailable"
    finally:
        if container_id:
            try:
                subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, timeout=10, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for thread in (writer, reader):
            if thread:
                thread.join(timeout=5)
        if process:
            process.stdin.close()
            process.stdout.close()
    text = output.decode("utf-8", errors="replace")
    if len(text.encode()) > MAX_OUTPUT:
        text = text.encode()[:MAX_OUTPUT].decode("utf-8", errors="ignore")
        truncated = True
    return {"state": state, "exit_code": code, "output": text, "output_truncated": truncated, "error_code": error}
