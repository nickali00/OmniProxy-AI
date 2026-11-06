"""Verify the live HTTPS boundary using explicit trust, never insecure TLS."""

import argparse
import json
import socket
import ssl
import warnings
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def verify(args):
    if not __debug__:
        raise RuntimeError("Run security verification without Python optimization")
    parsed = urlsplit(args.url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Use an HTTPS origin without credentials or path")
    context = ssl.create_default_context(cafile=str(args.ca_file))
    protocols = []
    for version in (ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3):
        check = ssl.create_default_context(cafile=str(args.ca_file))
        check.minimum_version = check.maximum_version = version
        with socket.create_connection((parsed.hostname, parsed.port or 443), timeout=5) as connection, check.wrap_socket(connection, server_hostname=parsed.hostname) as secure:
            protocols.append(secure.version())
    rejected = []
    for version in (ssl.TLSVersion.TLSv1, ssl.TLSVersion.TLSv1_1):
        check = ssl.create_default_context(cafile=str(args.ca_file))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            check.minimum_version = check.maximum_version = version
        check.set_ciphers("ALL:@SECLEVEL=0")
        try:
            with socket.create_connection((parsed.hostname, parsed.port or 443), timeout=5) as connection, check.wrap_socket(connection, server_hostname=parsed.hostname):
                raise AssertionError("Legacy TLS was accepted")
        except ssl.SSLError as error:
            if error.reason not in {"TLSV1_ALERT_PROTOCOL_VERSION", "UNSUPPORTED_PROTOCOL"}:
                raise
            rejected.append(version.name)
    report = {"tls_verified": True, "accepted_protocols": protocols, "rejected_protocols": rejected}
    with httpx.Client(base_url=args.url, verify=context, trust_env=False, timeout=120, follow_redirects=False) as client:
        assert client.get("/healthz").status_code == 200
        for path in ("/v1/models", "/api/gateway-apis", "/admin/v1/me"):
            response = client.get(path)
            assert response.status_code == 401, (path, response.status_code)
        assert client.get("/").status_code == 303
        response = client.get("/admin/login")
        assert response.status_code == 200 and response.headers.get("strict-transport-security") == "max-age=31536000"
        assert response.headers.get("x-content-type-options") == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "server" not in response.headers
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404
        report.update(unauthenticated_admin_denied=True, response_headers_verified=True)
        if args.api_key_file:
            key = json.loads(args.api_key_file.read_text())["api_key"]
            auth = {"Authorization": "Bearer " + key}
            assert client.get("/v1/models", headers=auth).status_code == 200
            forged = client.get("/api/gateway-apis", headers={**auth, "X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https"})
            assert forged.status_code == 401
            response = client.post("/v1/chat/completions", headers=auth, json={"model": args.model, "messages": [{"role": "user", "content": "Say only OK."}], "max_completion_tokens": 64, "stream": True})
            assert response.status_code == 200 and response.text.endswith("data: [DONE]\n\n")
            report.update(application_key_still_valid=True, real_sse_over_verified_tls=True)
        if args.admin_session_file:
            session = json.loads(args.admin_session_file.read_text())
            auth = {"Authorization": "Bearer " + session["token"]}
            response = client.post("/admin/v1/session", headers=auth)
            assert response.status_code == 200
            cookie = response.headers["set-cookie"]
            assert all(value in cookie for value in ("Secure", "HttpOnly", "SameSite=strict"))
            assert client.get("/").status_code == 200
            assert client.get("/api/gateway-apis").status_code == 200
            assert client.post("/api/gateway-apis/groups", json={"name": "CSRF rejected fixture"}).status_code == 403
            report.update(secure_browser_session=True, csrf_rejected=True)
            if args.revoke_session:
                response = client.delete("/admin/v1/sessions/" + session["session_id"], headers=auth)
                assert response.status_code == 204
                assert client.get("/api/gateway-apis").status_code == 401
                report["test_session_revoked"] = True
    if args.http_origin:
        with httpx.Client(base_url=args.http_origin, trust_env=False, follow_redirects=False, timeout=10) as client:
            response = client.get("/")
            assert response.status_code == 308 and response.headers["location"].startswith(args.url.rstrip("/"))
            for method in ("GET", "POST"):
                response = client.request(method, "/v1/models")
                assert response.status_code == 426
        report["http_browser_redirect_and_api_refusal"] = True
    return {"scope": "https_boundary", "passed": True, **report}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--ca-file", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--admin-session-file", type=Path)
    parser.add_argument("--revoke-session", action="store_true")
    parser.add_argument("--http-origin")
    parser.add_argument("--model", default="local")
    args = parser.parse_args()
    print(json.dumps(verify(args), indent=2))
