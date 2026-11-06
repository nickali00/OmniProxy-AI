import importlib.util
import io
import os
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import settings
from app.errors import GatewayError
from app.identity import provision
from app.main import app
from app.network_security import enforce_upstream_transport
from app.providers.ollama import OllamaProvider
from app.security import harden_storage_permissions
from scripts.prepare_https import configuration, prepare

KEY = "sk-local-security-synthetic-at-least-thirty-two-characters"


@pytest.fixture
def secure(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "database_path", tmp_path / "security.sqlite3")
    monkeypatch.setattr(settings, "provider_vault_key_path", tmp_path / "vault.key")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)
    monkeypatch.setattr(settings, "admin_auth_required", False)
    monkeypatch.setattr(settings, "dashboard_auth_required", True)
    monkeypatch.setattr(settings, "require_https", True)
    monkeypatch.setattr(settings, "otel_enabled", False)
    with TestClient(app, base_url="https://127.0.0.1") as client:
        yield client


def test_dashboard_login_preserves_personal_api_keys_and_sets_secure_cookie(secure):
    assert secure.get("/api/gateway-apis").status_code == 401
    assert secure.get("/readyz").status_code == 401
    assert secure.get("/", follow_redirects=False).headers["location"] == "/admin/login"
    auth = {"Authorization": "Bearer " + KEY}
    response = secure.get("/v1/models", headers=auth)
    assert response.status_code == 200
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    operator = secure.portal.call(partial(provision, "Security operator", operator=True))
    session = secure.post("/admin/v1/session", headers={"Authorization": "Bearer " + operator["token"]})
    cookie = session.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert secure.get("/").status_code == 200
    assert operator["token"] not in secure.get("/").text
    assert secure.post("/api/gateway-apis/groups", json={"name": "CSRF"}).status_code == 403
    headers = {"Origin": "https://127.0.0.1", "X-OmniProxy-Request": "dashboard"}
    assert secure.post("/api/gateway-apis/groups", headers=headers, json={"name": "Allowed"}).status_code == 201


def test_cleartext_and_forwarded_header_spoof_are_rejected(secure):
    response = secure.get("http://127.0.0.1/v1/models", headers={"X-Forwarded-Proto": "https", "Authorization": "Bearer " + KEY})
    assert response.status_code == 426 and response.json()["error"]["code"] == "https_required"
    assert secure.get("http://127.0.0.1/healthz").status_code == 200
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert secure.get(path).status_code == 404
    assert secure.get("/v1/models", headers={"Host": "untrusted.example", "Authorization": "Bearer " + KEY}).status_code == 400


def test_authentication_throttle_cannot_be_evaded_by_forged_xff(secure, monkeypatch):
    monkeypatch.setattr(settings, "auth_failures_per_minute", 5)
    for index in range(5):
        assert secure.get("/v1/models", headers={"X-Forwarded-For": f"198.51.100.{index}"}).status_code == 401
    blocked = secure.get("/v1/models", headers={"X-Forwarded-For": "203.0.113.1"})
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "authentication_rate_limited"
    assert int(blocked.headers["retry-after"]) > 0


def test_admin_body_is_bounded_and_cookie_pages_have_security_headers(secure):
    operator = secure.portal.call(partial(provision, "Body operator", operator=True))
    response = secure.post("/api/gateway-apis/groups", content=b"x", headers={"Authorization": "Bearer " + operator["token"], "Content-Length": str(3 * 1024 * 1024), "Origin": "https://127.0.0.1", "X-OmniProxy-Request": "dashboard"})
    assert response.status_code == 413
    login = secure.get("/admin/login")
    assert login.headers["x-frame-options"] == "DENY"
    assert login.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in login.headers["content-security-policy"]


def test_image_codec_allowlist_rejects_jpeg2000_disguised_as_png(secure):
    content = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(content, format="JPEG2000")
    uploaded = secure.post("/v1/files", headers={"Authorization": "Bearer " + KEY}, files={"file": ("disguised.png", content.getvalue(), "image/png")})
    assert uploaded.status_code == 400


def test_https_profile_removes_gateway_port_and_never_disables_tls_verification(tmp_path):
    caddy, overlay = configuration("192.0.2.10", "192.0.2.10")
    assert "ports: !reset []" in overlay
    assert "GATEWAY_UDS" in overlay and "security_ingress:/run/gateway:ro" in overlay
    assert "tls1.2 tls1.3" in caddy and "issuer internal" in caddy
    assert "lifetime 1826d" in caddy and "intermediate_lifetime 2192d" in caddy
    assert "X-Forwarded-Proto https" in caddy and "header_up -Forwarded" in caddy
    assert "tls_insecure_skip_verify" not in caddy and "admin off" in caddy
    directory = prepare(tmp_path / "profile", "192.0.2.10", "192.0.2.10", docker_gid=125)
    assert directory.stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        prepare(directory, "192.0.2.10", "192.0.2.10", docker_gid=125)
    for host in ("*", "0.0.0.0", "host\nrespond secret", "host/path", "user@host"):
        with pytest.raises(ValueError):
            configuration(host, "192.0.2.10")


def test_privileged_runner_fails_closed_and_accepts_only_its_control_secret(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("maintenance_security_fixture", Path("maintenance-runner/server.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("MAINTENANCE_TOKEN_PATH", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client:
            assert client.get("/healthz").status_code == 200
            assert client.post("/v1/codex/update", json={"version": "0.0.0"}).status_code == 503
            token = "test-control-" + "a" * 48
            file = tmp_path / "control.token"
            file.write_text(token)
            os.chmod(file, 0o600)
            monkeypatch.setenv("MAINTENANCE_TOKEN_PATH", str(file))
            assert client.post("/v1/codex/update", json={"version": "0.0.0"}).status_code == 401
            # Valid authentication reaches validation; invalid commands never run.
            assert client.post("/v1/commands", headers={"Authorization": "Bearer " + token}, json={"commands": ["forbidden"]}).status_code == 403
            assert not module.jobs
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_public_plaintext_and_metadata_upstreams_are_rejected(monkeypatch):
    monkeypatch.setattr(settings, "require_https", True)
    for url in ("http://8.8.8.8/v1", "http://169.254.169.254/latest", "http://0.0.0.0/v1"):
        with pytest.raises(GatewayError):
            await enforce_upstream_transport(url)
    await enforce_upstream_transport("http://127.0.0.1:11434")
    await enforce_upstream_transport("https://public.example/v1")


def test_provider_error_does_not_reflect_private_upstream_details():
    marker = "PRIVATE_UPSTREAM_ERROR_WITH_PROMPT"
    error = OllamaProvider._provider_error(RuntimeError(marker))
    assert marker not in error.message and error.code == "ollama_error"


def test_secure_storage_restricts_existing_database_and_wal(tmp_path, monkeypatch):
    path = tmp_path / "existing.sqlite3"
    paths = [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]
    for item in paths:
        item.write_bytes(b"synthetic storage")
        item.chmod(0o644)
    monkeypatch.setattr(settings, "database_path", path)
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "require_https", True)
    harden_storage_permissions()
    assert all(item.stat().st_mode & 0o777 == 0o600 for item in paths)


def test_remote_postgres_requires_verified_tls_in_secure_mode(monkeypatch):
    import sqlite3

    from app.storage_postgres import validate_transport
    monkeypatch.setattr(settings, "require_https", True)
    with pytest.raises(sqlite3.OperationalError):
        validate_transport("postgresql://example.invalid/db?sslmode=prefer")
    validate_transport("postgresql://example.invalid/db?sslmode=verify-full")
    validate_transport("postgresql://postgres/db")


def test_disguised_media_playlist_cannot_make_network_requests(secure):
    requests = []

    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        playlist = f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1:{server.server_port}/fixture.ts\n#EXT-X-ENDLIST\n"
        response = secure.post("/v1/files", headers={"Authorization": "Bearer " + KEY}, files={"file": ("disguised.mp4", playlist.encode(), "video/mp4")})
        assert response.status_code == 400
        assert requests == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
