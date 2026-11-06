"""Bounded file access for registered agent workspaces; no shell execution."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from app.config import settings
from app.errors import GatewayError

MAX_FILE_BYTES = 256_000
MAX_SNAPSHOT_BYTES = 1_000_000
MAX_FILES = 50


@contextmanager
def workspace_lock(workspace_id: str):
    directory = Path(settings.database_path).parent / "job-locks"
    directory.mkdir(mode=0o700, exist_ok=True)
    # One writer across registrations also covers nested or overlapping roots.
    descriptor = os.open(directory / "workspace-writer.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GatewayError(409, "Workspace occupato.", code="job_conflict") from exc
        yield
    finally:
        os.close(descriptor)


def failure(code: str, status: int = 400) -> GatewayError:
    return GatewayError(status, {
        "workspace_not_allowed": "Workspace non autorizzato.",
        "workspace_changed": "Il workspace registrato è cambiato.",
        "unsafe_workspace_path": "Percorso workspace non consentito.",
        "workspace_limit_exceeded": "Limiti del workspace superati.",
        "stale_workspace": "I file sono cambiati dopo la proposta.",
        "workspace_write_failed": "Scrittura workspace non riuscita.",
        "workspace_recovery_required": "Il workspace richiede verifica dopo una scrittura interrotta.",
    }.get(code, "Operazione workspace non disponibile."), code=code)


def clean_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or len(value) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value or path.is_absolute() or str(path) != value or any(part in {".", "..", ".git", ".ssh", ".aws", ".codex"} for part in path.parts):
        raise failure("unsafe_workspace_path")
    for part in path.parts:
        lowered = part.lower()
        if lowered.startswith(".env") or lowered in {"auth.json", "credentials", "credentials.json", "id_rsa", "id_ed25519"} or lowered.endswith((".pem", ".key", ".p12", ".pfx")):
            raise failure("unsafe_workspace_path")
    return value


def digest(content: str | None) -> str | None:
    return hashlib.sha256(content.encode()).hexdigest() if content is not None else None


@contextmanager
def _absolute_directory(path: str):
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            for part in Path(path).parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        except OSError as exc:
            raise failure("unsafe_workspace_path") from exc
        yield descriptor
    finally:
        os.close(descriptor)


def register_root(path: str, paths: list[str]) -> dict:
    root = Path(path)
    if any(ord(char) < 32 for char in path) or not root.is_absolute() or str(root) != path or str(root.resolve()) != path:
        raise failure("workspace_not_allowed")
    allowed = [Path(item).resolve() for item in settings.job_workspace_roots.split(os.pathsep) if item]
    if not any(root == parent or root.is_relative_to(parent) for parent in allowed):
        raise failure("workspace_not_allowed", 403)
    paths = list(dict.fromkeys(clean_path(item) for item in paths))
    if not paths or len(paths) > MAX_FILES:
        raise failure("workspace_limit_exceeded")
    with _absolute_directory(path) as descriptor:
        metadata = os.fstat(descriptor)
    record = {"root": path, "device": metadata.st_dev, "inode": metadata.st_ino, "paths": paths}
    snapshot(record, paths)
    return record


@contextmanager
def open_root(workspace: dict):
    with _absolute_directory(workspace["root"]) as descriptor:
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != (workspace["device"], workspace["inode"]):
            raise failure("workspace_changed", 409)
        yield descriptor


@contextmanager
def _parent(root_fd: int, path: str):
    pieces = PurePosixPath(clean_path(path)).parts
    descriptor = os.dup(root_fd)
    device = os.fstat(root_fd).st_dev
    try:
        try:
            for part in pieces[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                if os.fstat(descriptor).st_dev != device:
                    raise failure("unsafe_workspace_path")
        except OSError as exc:
            raise failure("unsafe_workspace_path") from exc
        yield descriptor, pieces[-1]
    finally:
        os.close(descriptor)


def _read(parent_fd: int, leaf: str) -> tuple[str | None, int]:
    try:
        descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    except FileNotFoundError:
        return None, 0o644
    except OSError as exc:
        raise failure("unsafe_workspace_path") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise failure("unsafe_workspace_path")
        if metadata.st_size > MAX_FILE_BYTES:
            raise failure("workspace_limit_exceeded")
        content = os.read(descriptor, MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise failure("workspace_limit_exceeded")
        if b"\x00" in content:
            raise failure("unsafe_workspace_path")
        return content.decode("utf-8"), stat.S_IMODE(metadata.st_mode) & 0o777
    except UnicodeError as exc:
        raise failure("unsafe_workspace_path") from exc
    finally:
        os.close(descriptor)


def snapshot(workspace: dict, paths: list[str]) -> dict[str, dict]:
    if not paths or len(paths) > MAX_FILES or any(path not in workspace["paths"] for path in paths):
        raise failure("workspace_not_allowed", 403)
    root_path = Path(workspace["root"])
    for mount in _mountpoints():
        if mount != root_path and mount.is_relative_to(root_path) and any((root_path / path).is_relative_to(mount) for path in paths):
            raise failure("unsafe_workspace_path")
    result = {}
    total = 0
    with open_root(workspace) as root:
        for path in paths:
            with _parent(root, path) as (parent, leaf):
                content, mode = _read(parent, leaf)
                total += len(content.encode()) if content is not None else 0
                if total > MAX_SNAPSHOT_BYTES:
                    raise failure("workspace_limit_exceeded")
                result[path] = {"content": content, "hash": digest(content), "mode": mode}
    return result


def _mountpoints() -> list[Path]:
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
        return [Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), line.split()[4])) for line in lines]
    except OSError as exc:
        raise failure("unsafe_workspace_path") from exc


def _write(root: int, path: str, content: str | None, expected_hash: str | None, mode: int):
    with _parent(root, path) as (parent, leaf):
        current, _ = _read(parent, leaf)
        if digest(current) != expected_hash:
            raise failure("stale_workspace", 409)
        if content is None:
            os.unlink(leaf, dir_fd=parent)
            os.fsync(parent)
            return
        temporary = f".omniproxy-{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content.encode())
                os.fchmod(output.fileno(), mode)
                output.flush()
                os.fsync(output.fileno())
            if current is None:
                os.link(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            else:
                os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def restore(workspace: dict, base: dict, changes: list[dict]) -> None:
    """Undo only writes still matching our journal; preserve external edits."""
    with open_root(workspace) as root:
        for change in reversed(changes):
            path = change["path"]
            with _parent(root, path) as (parent, leaf):
                current, _ = _read(parent, leaf)
            if digest(current) == base[path]["hash"]:
                continue
            if digest(current) != digest(change["content"]):
                raise failure("workspace_recovery_required", 409)
            _write(root, path, base[path]["content"], digest(change["content"]), base[path]["mode"])


def apply(workspace: dict, base: dict, changes: list[dict]) -> None:
    current = snapshot(workspace, list(base))
    if any(current[path]["hash"] != item["hash"] for path, item in base.items()):
        raise failure("stale_workspace", 409)
    completed = []
    try:
        with open_root(workspace) as root:
            for change in changes:
                path = change["path"]
                _write(root, path, change["content"], base[path]["hash"], base[path]["mode"])
                completed.append(change)
    except BaseException:
        restore(workspace, base, completed)
        raise
