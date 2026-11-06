import asyncio
import socket
import sqlite3
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request

from app.config import settings
from app.main import app
from app.routing import ModelRoute

KEY = "sk-local-test-key-with-at-least-thirty-two-characters"


@asynccontextmanager
async def running(server_app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(server_app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("use_tools", [False, True])
async def test_http_disconnect_cancels_gateway_broker_request(tmp_path, monkeypatch, stream, use_tools):
    started = asyncio.Event()
    disconnected = asyncio.Event()
    broker = FastAPI()

    @broker.post("/v1/chat")
    async def chat(request: Request):
        await request.json()
        started.set()
        async with asyncio.timeout(5):
            while not await request.is_disconnected():
                await asyncio.sleep(0.02)
        disconnected.set()
        return {"content": "", "tool_calls": []}

    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)
    monkeypatch.setattr("app.main.resolve_model_route", lambda model: ModelRoute(model, "codex", "test-model"))
    async with running(broker) as broker_url:
        monkeypatch.setattr(settings, "codex_broker_url", broker_url)
        async with running(app) as gateway_url:
            async with httpx.AsyncClient(base_url=gateway_url) as client:
                task = asyncio.create_task(client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {KEY}"}, json={
                    "model": "test", "stream": stream,
                    "messages": [{"role": "user", "content": "Use client tool"}],
                    **({"tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]} if use_tools else {}),
                }))
                await asyncio.wait_for(started.wait(), 3)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.wait_for(disconnected.wait(), 3)
            async with asyncio.timeout(3):
                while True:
                    with sqlite3.connect(settings.database_path) as connection:
                        row = connection.execute("SELECT status_code, error_code FROM usage_logs").fetchone()
                    if row is not None:
                        break
                    await asyncio.sleep(0.02)
            assert row == (499, "client_disconnected")
