import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse

from app.config import settings
from app.request_lifecycle import RequestLimitsMiddleware


async def invoke(messages, *, headers=(), path="/v1/chat/completions", slots=None):
    received, sent = [], []
    scope = {"type": "http", "path": path, "method": "POST", "headers": list(headers), "app": SimpleNamespace(state=SimpleNamespace(api_request_slots=slots or asyncio.Semaphore(1)))}
    queue = list(messages)

    async def receive():
        if queue:
            return queue.pop(0)
        await asyncio.sleep(60)

    async def endpoint(scope, receive, send):
        body = await receive()
        received.append(body)
        await JSONResponse({"size": len(body["body"])})(scope, receive, send)

    await RequestLimitsMiddleware(endpoint)(scope, receive, send_result(sent))
    return received, sent


def send_result(sent):
    async def send(message):
        sent.append(message)
    return send


@pytest.mark.asyncio
async def test_chunked_body_is_bounded_before_endpoint_and_not_echoed(monkeypatch):
    monkeypatch.setattr(settings, "max_request_body_bytes", 10)
    accepted, sent = await invoke([{"type": "http.request", "body": b"abc", "more_body": True}, {"type": "http.request", "body": b"def", "more_body": False}])
    assert accepted[0]["body"] == b"abcdef" and sent[0]["status"] == 200
    accepted, sent = await invoke([{"type": "http.request", "body": b"private-content", "more_body": False}])
    assert not accepted and sent[0]["status"] == 413
    assert b"private" not in sent[1]["body"]


@pytest.mark.asyncio
async def test_content_length_upload_requirement_and_mismatch(monkeypatch):
    monkeypatch.setattr(settings, "max_request_body_bytes", 10)
    for headers, body, status in [([(b"content-length", b"11")], b"", 413), ([(b"content-length", b"bad")], b"", 400), ([(b"content-length", b"2")], b"x", 400), ([(b"content-length", b"1"), (b"content-length", b"1")], b"x", 400)]:
        accepted, sent = await invoke([{"type": "http.request", "body": body}], headers=headers)
        assert not accepted and sent[0]["status"] == status
    accepted, sent = await invoke([], path="/v1/files")
    assert not accepted and sent[0]["status"] == 411


@pytest.mark.asyncio
async def test_slow_body_and_busy_gateway_release_slots(monkeypatch):
    monkeypatch.setattr(settings, "request_body_timeout_seconds", 0.01)
    slots = asyncio.Semaphore(1)
    accepted, sent = await invoke([], slots=slots)
    assert not accepted and sent[0]["status"] == 408 and slots._value == 1
    await slots.acquire()
    accepted, sent = await invoke([], slots=slots)
    assert not accepted and sent[0]["status"] == 429
    assert json.loads(sent[1]["body"])["error"]["code"] == "gateway_busy"
    slots.release()
    accepted, sent = await invoke([{"type": "http.disconnect"}], slots=slots)
    assert not accepted and not sent and slots._value == 1
