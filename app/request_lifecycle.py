"""Propagate disconnects while an HTTP route is awaiting a provider."""

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from fastapi import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings
from app.errors import GatewayError, openai_error_body

T = TypeVar("T")


class RequestLimitsMiddleware:
    """Bound API memory before JSON/multipart parsing, including chunked bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(("/v1/", "/v1beta/", "/admin/v1/", "/api/")):
            await self.app(scope, receive, send)
            return

        async def reject(status, code):
            response = JSONResponse(status_code=status, content=openai_error_body("Richiesta oltre i limiti tecnici del gateway.", error_type="rate_limit_error" if status == 429 else "invalid_request_error", code=code), headers={"Retry-After": "1"} if status == 429 else None)
            await response(scope, receive, send)

        headers = [value for name, value in scope["headers"] if name.lower() == b"content-length"]
        try:
            declared = int(headers[0]) if headers else None
            if len(headers) > 1 or (declared is not None and declared < 0):
                raise ValueError
        except ValueError:
            await reject(400, "invalid_content_length")
            return
        maximum = settings.max_request_body_bytes
        if scope["path"].startswith(("/admin/", "/api/")):
            maximum = min(maximum, 2 * 1024 * 1024)
        upload = scope["method"] == "POST" and scope["path"] in {"/v1/files", "/v1/audio/transcriptions"}
        if upload:
            maximum = min(maximum, settings.file_max_bytes + 1024 * 1024)
            if declared is None:
                await reject(411, "file_upload_size_limit")
                return
        if declared is not None and declared > maximum:
            await reject(413, "file_upload_size_limit" if upload else "request_body_too_large")
            return
        slots = scope["app"].state.api_request_slots
        try:
            await asyncio.wait_for(slots.acquire(), timeout=0.1)
        except TimeoutError:
            await reject(429, "gateway_busy")
            return
        try:
            # A single bounded bytearray prevents tiny-chunk object amplification.
            body = bytearray()
            try:
                async with asyncio.timeout(settings.request_body_timeout_seconds):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > maximum:
                            await reject(413, "file_upload_size_limit" if upload else "request_body_too_large")
                            return
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await reject(408, "request_body_timeout")
                return
            if declared is not None and declared != len(body):
                await reject(400, "invalid_content_length")
                return
            replayed = False

            async def replay():
                nonlocal replayed, body
                if not replayed:
                    replayed = True
                    data, body = bytes(body), bytearray()
                    return {"type": "http.request", "body": data, "more_body": False}
                return await receive()

            await self.app(scope, replay, send)
        finally:
            slots.release()


class ClientDisconnectMiddleware:
    """Observe the socket independently of BaseHTTPMiddleware receive wrappers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        disconnected = asyncio.Event()
        scope.setdefault("state", {})["client_disconnected_event"] = disconnected
        messages: asyncio.Queue = asyncio.Queue(maxsize=1)

        async def observe():
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected.set()
                    await messages.put(message)
                    return
                await messages.put(message)

        async def forwarded_receive():
            if disconnected.is_set() and messages.empty():
                return {"type": "http.disconnect"}
            return await messages.get()

        observer = asyncio.create_task(observe())
        try:
            await self.app(scope, forwarded_receive, send)
        finally:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)


async def await_connected(request: Request, operation: Awaitable[T]) -> T:
    task = asyncio.ensure_future(operation)
    disconnected = request.scope.get("state", {}).get("client_disconnected_event")
    watcher = asyncio.create_task(disconnected.wait()) if disconnected else None
    try:
        while not task.done():
            done, _ = await asyncio.wait({task, watcher} if watcher else {task}, timeout=0.1)
            if task in done:
                break
            if (watcher is not None and watcher in done) or (watcher is None and await request.is_disconnected()):
                raise GatewayError(499, "Client disconnesso.", code="client_disconnected")
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
