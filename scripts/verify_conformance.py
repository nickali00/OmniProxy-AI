"""Run standalone conformance tools against a real loopback HTTP gateway and Ollama."""

import argparse
import asyncio
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

import uvicorn

from app.config import settings
from app.database import create_api_key
from app.main import app


def write_private_key(path, key):
    with open(path, "x", opener=lambda file, flags: os.open(file, flags, 0o600)) as target:
        json.dump({"api_key": key}, target)


async def subprocess_check(module, args, env):
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", module, *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=360)
        if process.returncode:
            # Neither SDK errors nor server response bodies belong in a public report.
            if module == "conformance.check":
                report = json.loads(stdout)
                failed = [item["name"] for item in report["checks"] if not item["passed"]]
                raise RuntimeError("Core conformance failed: " + ", ".join(failed))
            raise RuntimeError(module + " failed; rerun its checks with synthetic keys")
        return json.loads(stdout)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def verify(args):
    with tempfile.TemporaryDirectory(prefix="omniproxy-conformance-") as temporary:
        root = Path(temporary)
        settings.database_url = ""
        settings.admin_auth_required = False
        settings.dashboard_auth_required = False
        settings.require_https = False
        settings.otel_enabled = False
        settings.database_path = root / "test.sqlite3"
        settings.provider_vault_key_path = root / "vault.key"
        settings.bootstrap_api_key = "sk-local-conformance-synthetic-at-least-thirty-two-characters"
        settings.ollama_base_url = args.ollama_url
        settings.ollama_model = args.ollama_model
        async with app.router.lifespan_context(app):
            _, other = await create_api_key("Conformance other owner")
            for name, key in [("key.json", settings.bootstrap_api_key), ("other.json", other)]:
                path = root / name
                write_private_key(path, key)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
                server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", access_log=False, log_level="warning"))
                task = asyncio.create_task(server.serve(sockets=[sock]))
                try:
                    async with asyncio.timeout(10):
                        while not server.started:
                            if task.done():
                                await task
                                raise RuntimeError("HTTP server did not start")
                            await asyncio.sleep(0.05)
                    common = ["--base-url", f"http://127.0.0.1:{port}", "--key-file", str(root / "key.json"), "--model", "local"]
                    env = os.environ.copy()
                    core = await subprocess_check("conformance.check", [*common, "--other-key-file", str(root / "other.json")], env)
                    reports = [core]
                    if args.sdk_path:
                        env["PYTHONPATH"] = os.pathsep.join(str(path.resolve()) for path in args.sdk_path)
                        reports.append(await subprocess_check("conformance.sdk_checks", common, env))
                    return {"release": settings.app_version, "contract_version": "1", "scope": "loopback_http_isolated_sqlite_real_ollama", "provider_model": args.ollama_model, "passed": all(report["passed"] for report in reports), "reports": reports}
                finally:
                    server.should_exit = True
                    await task


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--ollama-model", default="qwen3.5:2b")
    parser.add_argument("--sdk-path", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(verify(args))
    report = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(report)
    print(report)


if __name__ == "__main__":
    main()
