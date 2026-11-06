"""Transport policy, bounded authentication throttling and internal control auth."""

import time
from collections import OrderedDict
from pathlib import Path

import httpx
from starlette.responses import JSONResponse

from app.config import settings
from app.errors import openai_error_body


def harden_storage_permissions():
    if settings.require_https and not settings.database_url:
        database = Path(settings.database_path)
        for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
            if path.exists():
                if path.is_symlink():
                    raise RuntimeError("Secure storage cannot use symbolic links")
                path.chmod(0o600)


async def maintenance_authorization(request):
    try:
        path = settings.maintenance_token_path
        if path is None:
            raise ValueError()
        token = path.read_text().strip()
        if not 43 <= len(token) <= 128 or not all(char.isalnum() or char in "_-" for char in token):
            raise ValueError()
    except (OSError, ValueError):
        raise httpx.RequestError("Maintenance authorization is not configured.", request=request) from None
    request.headers["Authorization"] = "Bearer " + token


class SecurityBoundaryMiddleware:
    """Limits are process-local and based on the trusted ASGI peer, never raw XFF."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        state = scope["app"].state
        failures = getattr(state, "authentication_failures", None)
        if failures is None:
            failures = state.authentication_failures = OrderedDict()
        if settings.require_https and scope["scheme"] != "https" and path != "/healthz":
            response = JSONResponse(openai_error_body("Use the HTTPS endpoint.", error_type="invalid_request_error", code="https_required"), status_code=426, headers={"Cache-Control": "no-store"})
            await response(scope, receive, send)
            return
        if path in {"/docs", "/redoc", "/openapi.json"} and (settings.dashboard_auth_required or settings.admin_auth_required):
            response = JSONResponse({"detail": "Not Found"}, status_code=404)
            await response(scope, receive, send)
            return
        category = "admin" if path in {"/admin", "/readyz"} or path.startswith(("/admin/v1/", "/api/")) else "api" if path.startswith(("/v1/", "/v1beta/")) else None
        peer = (scope.get("client") or ("unix",))[0]
        key = (str(peer)[:128], category)
        now = time.monotonic()
        for item, (_, start) in list(failures.items()):
            if now - start >= 60:
                failures.pop(item, None)
        count, started = failures.get(key, (0, now))
        if category and count >= settings.auth_failures_per_minute:
            response = JSONResponse(openai_error_body("Too many authentication failures. Try again later.", error_type="rate_limit_error", code="authentication_rate_limited"), status_code=429, headers={"Retry-After": str(max(1, int(60 - (now - started)))), "Cache-Control": "no-store"})
            await response(scope, receive, send)
            return

        async def guarded_send(message):
            if message["type"] == "http.response.start":
                if category and message["status"] in {401, 403}:
                    current, start = failures.get(key, (0, time.monotonic()))
                    failures[key] = (current + 1, start)
                    failures.move_to_end(key)
                    while len(failures) > 1024:
                        failures.popitem(last=False)
                headers = message.setdefault("headers", [])
                existing = {name.lower() for name, _ in headers}
                extra = {
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"no-referrer",
                    b"permissions-policy": b"camera=(), microphone=(), geolocation=(), payment=()",
                }
                if path.startswith(("/admin", "/v1/", "/v1beta/")):
                    extra[b"cache-control"] = b"no-store"
                if path.startswith("/admin"):
                    extra[b"content-security-policy"] = b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
                if settings.require_https and scope["scheme"] == "https":
                    extra[b"strict-transport-security"] = b"max-age=31536000"
                headers.extend((name, value) for name, value in extra.items() if name not in existing)
            await send(message)

        await self.app(scope, receive, guarded_send)
