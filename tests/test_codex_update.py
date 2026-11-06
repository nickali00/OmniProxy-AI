import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _runner():
    path = Path(__file__).resolve().parents[1] / "maintenance-runner" / "server.py"
    spec = importlib.util.spec_from_file_location("omni_maintenance_runner_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_codex_update_pins_version_only_after_verified_rebuild(tmp_path, monkeypatch):
    runner = _runner()
    pin = tmp_path / "version.txt"
    pin.write_text("0.156.1\n")
    monkeypatch.setattr(runner, "CODEX_VERSION_FILE", str(pin))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    commands = []

    def success(args, **_kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=0,
            stdout="codex-cli 0.166.0\n" if args[2] == "exec" else "ok\n", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", success)
    runner.jobs["ok"] = {"id": "ok"}
    runner.run_codex_update("ok", "0.166.0")
    assert runner.jobs["ok"]["status"] == "completed"
    assert pin.read_text() == "0.166.0\n"
    assert [command[2] for command in commands] == ["build", "up", "exec"]
    assert commands[1][-1] == "codex-broker"

    pin.write_text("0.156.1\n")
    monkeypatch.setattr(runner.subprocess, "run", lambda *_args, **_kwargs:
        SimpleNamespace(returncode=1, stdout="", stderr="build failed"))
    runner.jobs["failed"] = {"id": "failed"}
    runner.run_codex_update("failed", "0.166.0")
    assert runner.jobs["failed"]["status"] == "failed"
    assert pin.read_text() == "0.156.1\n"
