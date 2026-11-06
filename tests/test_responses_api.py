import json
import sqlite3

from fastapi.testclient import TestClient

from app.config import settings
from app.database import hash_api_key
from app.main import app
from app.provider_broker import CodexBrokerClient
from app.routing import ModelRoute


KEY = "sk-local-test-key-with-at-least-thirty-two-characters"
HEADERS = {"Authorization": f"Bearer {KEY}"}


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)
    monkeypatch.setattr(settings, "external_mock_latency_seconds", 0)
    return TestClient(app, base_url="http://127.0.0.1")


def _events(body):
    return [
        (line[7:], json.loads(lines[index + 1][6:]))
        for lines in [body.splitlines()]
        for index, line in enumerate(lines)
        if line.startswith("event: ")
    ]


def test_responses_text_and_stream(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        missing = client.post("/v1/responses", json={"model": "reasoning-avanzato", "input": "ciao"})
        assert missing.status_code == 401
        response = client.post("/v1/responses", headers=HEADERS, json={"model": "reasoning-avanzato", "input": "ciao"})
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "response"
        assert body["status"] == "completed"
        assert body["output_text"] == body["output"][0]["content"][0]["text"]
        assert body["usage"]["total_tokens"] > 0
        streamed = client.post("/v1/responses", headers=HEADERS, json={"model": "reasoning-avanzato", "input": "ciao", "stream": True})
        assert streamed.status_code == 200
        events = _events(streamed.text)
        assert [kind for kind, _ in events][:2] == ["response.created", "response.in_progress"]
        assert all(kind == data["type"] for kind, data in events)
        assert [data["sequence_number"] for _, data in events] == list(range(1, len(events) + 1))
        assert events[-1][0] == "response.completed"
        assert events[-1][1]["response"]["usage"]["total_tokens"] > 0
        assert streamed.text.endswith("data: [DONE]\n\n")


def test_responses_function_call_and_history(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.resolve_model_route", lambda model: ModelRoute(model, "codex", "test-model"))

    async def fake_complete(self, request, *, model, reasoning_effort):
        assert request.messages[-2].tool_calls[0]["id"] == "call_previous"
        assert request.messages[-1].tool_call_id == "call_previous"
        assert request.tools[0]["function"]["name"] == "run_terminal"
        return {"content": None, "tool_calls": [{"id": "call_next", "type": "function", "function": {"name": "run_terminal", "arguments": '{"command":"pwd"}'}}]}

    monkeypatch.setattr(CodexBrokerClient, "complete_with_tools", fake_complete)
    payload = {"model": "test", "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "Continua"}]},
        {"type": "function_call", "call_id": "call_previous", "name": "run_terminal", "arguments": '{"command":"echo hi"}'},
        {"type": "function_call_output", "call_id": "call_previous", "output": "hi"},
    ], "tools": [{"type": "function", "name": "run_terminal", "description": "Client tool", "parameters": {"type": "object"}}]}
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/v1/responses", headers=HEADERS, json=payload)
        assert response.status_code == 200
        item = response.json()["output"][0]
        assert item["type"] == "function_call"
        assert item["call_id"] == "call_next"
        streamed = client.post("/v1/responses", headers=HEADERS, json={**payload, "stream": True})
        assert streamed.status_code == 200
        events = _events(streamed.text)
        assert any(kind == "response.function_call_arguments.done" for kind, _ in events)
        assert events[-1][1]["response"]["output"][0]["call_id"] == "call_next"


def test_responses_reject_unsupported_state_and_invalid_input(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        for extra in ({"previous_response_id": "resp_old"}, {"parallel_tool_calls": True}):
            response = client.post("/v1/responses", headers=HEADERS, json={"model": "local", "input": "ciao", **extra})
            assert response.status_code in {400, 404}
            assert response.json()["error"]["code"] in {"unsupported_response_feature", "response_not_found"}
        marker = "SECRET_RESPONSE_MARKER_53"
        response = client.post("/v1/responses", headers=HEADERS, json={"model": "local", "input": [{"role": "user", "content": [{"type": "input_text", "text": marker}, {"type": "input_audio", "data": marker}]}]})
        assert response.status_code == 400
        assert marker not in response.text


def test_responses_stored_continuity_and_owner_isolation(tmp_path, monkeypatch):
    seen = []

    async def fake_complete(self, request, resolved_model):
        seen.append([message.text_content() for message in request.messages])
        return "risposta uno" if len(seen) == 1 else "risposta due"

    from app.providers.cloud_mock import ExternalReasoningMockProvider
    monkeypatch.setattr(ExternalReasoningMockProvider, "complete", fake_complete)
    with _client(tmp_path, monkeypatch) as client:
        first = client.post("/v1/responses", headers=HEADERS, json={"model": "reasoning-avanzato", "input": "domanda uno", "store": True})
        assert first.status_code == 200
        response_id = first.json()["id"]
        assert first.json()["store"] is True
        retrieved = client.get(f"/v1/responses/{response_id}", headers=HEADERS)
        assert retrieved.json() == first.json()
        other_key = "sk-local-second-user-key-with-thirty-two-chars"
        with sqlite3.connect(tmp_path / "gateway.sqlite3") as connection:
            connection.execute("INSERT INTO api_keys (name, key_hint, key_hash) VALUES (?, ?, ?)", ("other", "second", hash_api_key(other_key)))
        other_headers = {"Authorization": f"Bearer {other_key}"}
        assert client.get(f"/v1/responses/{response_id}", headers=other_headers).status_code == 404
        assert client.delete(f"/v1/responses/{response_id}", headers=other_headers).status_code == 404
        assert client.post("/v1/responses", headers=other_headers, json={"model": "reasoning-avanzato", "previous_response_id": response_id, "input": "test"}).status_code == 404
        second = client.post("/v1/responses", headers=HEADERS, json={"model": "reasoning-avanzato", "previous_response_id": response_id, "input": "domanda due"})
        assert second.status_code == 200
        assert seen[-1] == ["domanda uno", "risposta uno", "domanda due"]
        assert second.json()["store"] is False
        assert second.json()["previous_response_id"] == response_id
        streamed = client.post("/v1/responses", headers=HEADERS, json={"model": "reasoning-avanzato", "previous_response_id": response_id, "input": "continua", "stream": True})
        response_events = _events(streamed.text)
        assert response_events[0][1]["response"]["previous_response_id"] == response_id
        assert response_events[-1][1]["response"]["previous_response_id"] == response_id
        not_stored = client.get(f"/v1/responses/{second.json()['id']}", headers=HEADERS)
        assert not_stored.status_code == 404
        deleted = client.delete(f"/v1/responses/{response_id}", headers=HEADERS)
        assert deleted.json() == {"id": response_id, "object": "response.deleted", "deleted": True}
        assert client.get(f"/v1/responses/{response_id}", headers=HEADERS).status_code == 404


def test_responses_streamed_store_and_continue(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        streamed = client.post("/v1/responses", headers=HEADERS, json={"model": "reasoning-avanzato", "input": "prima", "stream": True, "store": True})
        assert streamed.status_code == 200
        completed = _events(streamed.text)[-1][1]["response"]
        assert completed["store"] is True
        retrieved = client.get(f"/v1/responses/{completed['id']}", headers=HEADERS)
        assert retrieved.status_code == 200
        assert retrieved.json() == completed
        next_response = client.post("/v1/responses", headers=HEADERS, json={"model": "reasoning-avanzato", "previous_response_id": completed["id"], "input": "seconda"})
        assert next_response.status_code == 200
