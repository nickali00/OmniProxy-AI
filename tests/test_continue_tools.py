from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.provider_broker import CodexBrokerClient
from app.routing import ModelRoute
from app.schemas import ChatCompletionRequest


KEY = "sk-local-test-key-with-at-least-thirty-two-characters"


def test_gateway_preserves_tool_history_and_returns_openai_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)
    monkeypatch.setattr("app.main.resolve_model_route", lambda model: ModelRoute(model, "codex", "test-model"))

    async def fake_complete(self, request: ChatCompletionRequest, *, model, reasoning_effort):
        assert model == "test-model"
        assert request.messages[-2].tool_calls[0]["id"] == "call_previous"
        assert request.messages[-1].tool_call_id == "call_previous"
        assert request.tools[0]["function"]["name"] == "run_terminal"
        return {"content": None, "tool_calls": [{"id": "call_next", "type": "function",
            "function": {"name": "run_terminal", "arguments": '{"command":"pwd"}'}}]}

    monkeypatch.setattr(CodexBrokerClient, "complete_with_tools", fake_complete)
    body = {"model": "test", "messages": [
        {"role": "user", "content": "Continue"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_previous", "type": "function",
            "function": {"name": "run_terminal", "arguments": '{"command":"echo hi"}'}}]},
        {"role": "tool", "tool_call_id": "call_previous", "name": "run_terminal", "content": "hi"},
    ], "tools": [{"type": "function", "function": {"name": "run_terminal", "description": "Client tool",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}]}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {KEY}"}, json=body)
        assert response.status_code == 200
        assert response.json()["choices"][0]["finish_reason"] == "tool_calls"
        assert response.json()["choices"][0]["message"]["tool_calls"][0]["id"] == "call_next"
        streamed = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {KEY}"},
            json={**body, "stream": True})
        assert streamed.status_code == 200
        assert '"tool_calls"' in streamed.text
        assert '"finish_reason": "tool_calls"' in streamed.text
        assert streamed.text.endswith("data: [DONE]\n\n")
