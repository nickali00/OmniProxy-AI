"""Real MCP 2 transport + Codex orchestration, using a synthetic read-only tool."""

import argparse
import asyncio
import json
import logging
import socket
import tempfile
from pathlib import Path

import httpx
import uvicorn
from mcp.server.mcpserver import MCPServer

from app.config import settings
from app.database import create_gateway_api
from app.main import app


async def verify(broker_url, model):
    for name in ("httpx", "httpx2", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)
    native = MCPServer("OmniProxy fixture", version="1", log_level="WARNING")
    calls = 0

    @native.tool(description="Read the fixture's color. This tool only returns synthetic data.")
    def read_color() -> str:
        nonlocal calls
        calls += 1
        return "BLUE"

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(native.streamable_http_app(stateless_http=True, json_response=True), log_level="warning", access_log=False))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.05)
        with tempfile.TemporaryDirectory(prefix="omniproxy-mcp-e2e-") as temporary:
            settings.database_url = ""
            settings.database_path = Path(temporary) / "gateway.sqlite3"
            settings.admin_auth_required, settings.otel_enabled = False, False
            settings.bootstrap_api_key = "sk-local-mcp-e2e-key-with-thirty-two-characters"
            settings.codex_broker_url = broker_url
            async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", timeout=180) as client:
                registered = await client.post("/api/mcp/servers", headers={"Origin": "http://127.0.0.1", "X-OmniProxy-Request": "dashboard"}, json={"name": "Fixture MCP", "url": f"http://127.0.0.1:{port}/mcp", "auth_type": "none"})
                assert registered.status_code == 201, registered.text
                record = registered.json()
                assert record["status"] == "connected" and any(tool["name"] == "read_color" for tool in record["tools"])
                profile, key = await create_gateway_api(name="MCP E2E", provider="codex", model=model, reasoning_effort="low", mcp_tools=[(record["id"], "read_color")], limits={"token_budget": 10000, "budget_period": "daily"})
                headers = {"Authorization": "Bearer " + key}
                result = await client.post("/v1/chat/completions", headers=headers, json={"model": profile.slug, "messages": [{"role": "user", "content": "Use the provided read_color MCP tool exactly once and answer only the color it returns."}], "max_completion_tokens": 128})
                assert result.status_code == 200, result.text
                assert calls == 1 and "BLUE" in result.json()["choices"][0]["message"]["content"], {"calls": calls, "status": result.status_code}
                before = (await client.get("/v1/usage", headers=headers)).json()["usage"]
                conflict = await client.post("/v1/chat/completions", headers=headers, json={"model": profile.slug, "messages": [{"role": "user", "content": "Read color"}], "tools": [{"type": "function", "function": {"name": "client_tool", "parameters": {"type": "object"}}}]})
                assert conflict.status_code == 400 and conflict.json()["error"]["code"] == "tool_mode_conflict"
                after = (await client.get("/v1/usage", headers=headers)).json()["usage"]
                assert before["used_tokens"] == after["used_tokens"] and after["reserved_tokens"] == 0 and calls == 1
                print(json.dumps({"scope": "source_gateway_temporary_database_loopback_synthetic_server", "mcp_sdk_transport": "2.x", "model": model, "real_discovery_and_tool": True, "single_server_execution": True, "client_server_mode_conflict_rejected": True, "budget_settled": True, "passed": True}, indent=2))
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker-url", required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    arguments = parser.parse_args()
    asyncio.run(verify(arguments.broker_url, arguments.model))
