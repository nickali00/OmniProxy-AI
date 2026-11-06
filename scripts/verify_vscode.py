"""Run the installed Continue extension in an isolated VS Code test window."""

import argparse
import json
import os
import select
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx


@contextmanager
def virtual_display(binary):
    process = subprocess.Popen([binary, "-displayfd", "1", "-screen", "0", "1280x800x24", "-nolisten", "tcp", "-ac"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        if not select.select([process.stdout], [], [], 10)[0]:
            raise RuntimeError("Virtual display did not start")
        number = process.stdout.readline().strip()
        if not number.isdigit():
            raise RuntimeError("Invalid virtual display number")
        yield f":{number}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def verify(args):
    admin = {"Origin": args.gateway, "X-OmniProxy-Request": "dashboard"}
    with httpx.Client(base_url=args.gateway, timeout=30) as client:
        response = client.post("/api/gateway-apis", headers=admin, json={"name": f"IDE verification {uuid.uuid4().hex[:8]}", "provider": "codex", "model": args.model, "reasoning_effort": "low"})
        response.raise_for_status()
        profile = response.json()
        key = profile.pop("api_key")
        try:
            with tempfile.TemporaryDirectory(prefix="omniproxy-ide-") as temporary:
                root = Path(temporary)
                config = root / "continue"
                config.mkdir()
                (config / "config.yaml").write_text(
                    "name: OmniProxy IDE test\nversion: 1.0.0\nschema: v1\nmodels:\n"
                    "  - name: OmniProxy Codex\n    provider: openai\n"
                    f"    model: {profile['slug']}\n    apiBase: {args.gateway}/v1\n    apiKey: {key}\n"
                    "    useResponsesApi: false\n    capabilities: [tool_use]\n    roles: [chat, edit]\n"
                )
                (config / "config.yaml").chmod(0o600)
                workspace = root / "workspace"
                workspace.mkdir()
                (workspace / "marker.txt").write_text("IDE_FILE_MARKER_928\n")
                result_file = root / "result.json"
                # The package's NODE_ENV=test hook expects workers in out/llm.
                # Mirror the installed package without modifying it.
                installed = Path(args.extension)
                development = root / "extension"
                development.mkdir()
                for entry in installed.iterdir():
                    if entry.name != "out":
                        (development / entry.name).symlink_to(entry)
                (development / "out").mkdir()
                for entry in (installed / "out").iterdir():
                    target = development / "out" / entry.name
                    if entry.name == "extension.js":
                        os.link(entry, target)
                    else:
                        target.symlink_to(entry)
                (development / "out" / "llm").symlink_to(installed / "out")
                env = os.environ.copy()
                for inherited in ("VSCODE_IPC_HOOK_CLI", "ELECTRON_RUN_AS_NODE"):
                    env.pop(inherited, None)
                env.update(NODE_ENV="test", CONTINUE_GLOBAL_DIR=str(config), DISPLAY=args.display, OMNIPROXY_IDE_RESULT=str(result_file))
                result = subprocess.run([
                    "/usr/share/code/code", "--verbose", "--no-sandbox", "--disable-gpu", "--new-window", "--disable-workspace-trust", "--skip-welcome", "--skip-release-notes",
                    "--user-data-dir", str(root / "user"), "--extensions-dir", str(root / "extensions"),
                    "--extensionDevelopmentPath", str(development),
                    "--extensionTestsPath", str(Path(__file__).with_name("vscode-continue-test.cjs").resolve()), str(workspace),
                ], env=env, capture_output=True, text=True, timeout=180, check=False)
                if result.returncode or not result_file.exists():
                    output = (result.stdout + result.stderr).replace(key, "[REDACTED]")
                    raise RuntimeError(f"VS Code test failed ({result.returncode}): {output[-2500:]}")
                print(json.dumps(json.loads(result_file.read_text()), indent=2))
        finally:
            removed = client.delete(f"/api/gateway-apis/{profile['id']}", headers=admin)
            removed.raise_for_status()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", default="http://127.0.0.1:8181")
    parser.add_argument("--display", default=":1024")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--extension", default=str(Path.home() / ".vscode/extensions/continue.continue-2.0.0-linux-x64"))
    parser.add_argument("--xvfb")
    args = parser.parse_args()
    if args.xvfb:
        with virtual_display(args.xvfb) as display:
            args.display = display
            verify(args)
    else:
        verify(args)
