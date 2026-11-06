import json
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.providers.ollama import OllamaProvider
from app.token_counter import count_request_tokens, count_text_tokens

ADMIN = {"Origin": "http://127.0.0.1", "X-OmniProxy-Request": "dashboard"}
KEY = "sk-local-limits-test-key-with-thirty-two-characters"


def configure(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "limits.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)

    async def catalog(_app):
        return [{"id": "ollama", "connected": True, "models": [{"id": "fixture", "reasoning_efforts": ["off"]}]}]

    async def ollama(_app):
        return object()

    monkeypatch.setattr("app.main._model_catalog", catalog)
    monkeypatch.setattr("app.main._require_ollama_client", ollama)


def profile(client, limits):
    response = client.post("/api/gateway-apis", headers=ADMIN, json={"name": "Limited", "provider": "ollama", "model": "fixture", "reasoning_effort": "off", "limits": limits})
    assert response.status_code == 201, response.text
    return response.json(), {"Authorization": f"Bearer {response.json()['api_key']}"}


def test_output_cap_and_budget_accounting_on_normal_and_sse(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)

    async def complete(self, request, model):
        assert request.output_token_limit == 8
        return "parola " * 100

    async def stream(self, request, model):
        assert request.output_token_limit == 8
        for _ in range(100):
            yield "parola "

    monkeypatch.setattr(OllamaProvider, "complete", complete)
    monkeypatch.setattr(OllamaProvider, "stream", stream)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        record, headers = profile(client, {"output_tokens": 8, "token_budget": 100, "budget_period": "daily"})
        body = {"model": record["slug"], "messages": [{"role": "user", "content": "ciao"}], "max_completion_tokens": 1000}
        normal = client.post("/v1/chat/completions", headers=headers, json=body)
        assert normal.status_code == 200
        choice = normal.json()["choices"][0]
        assert count_text_tokens(choice["message"]["content"]) <= 8
        assert choice["finish_reason"] == "length"
        streamed = client.post("/v1/chat/completions", headers=headers, json={**body, "stream": True, "stream_options": {"include_usage": True}})
        assert streamed.status_code == 200
        chunks = [json.loads(line[6:]) for line in streamed.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
        text = "".join((chunk["choices"][0].get("delta") or {}).get("content", "") for chunk in chunks if chunk.get("choices"))
        assert count_text_tokens(text) <= 8
        assert any(chunk.get("choices") and chunk["choices"][0].get("finish_reason") == "length" for chunk in chunks)
        usage = client.get("/v1/usage", headers=headers).json()
        assert usage["usage"]["reserved_tokens"] == 0
        assert usage["usage"]["used_tokens"] == normal.json()["usage"]["total_tokens"] + chunks[-1]["usage"]["total_tokens"]
        listed = client.get("/api/gateway-apis").json()["data"][0]
        assert listed["limits"]["output_tokens"] == 8 and listed["token_usage"]["token_budget"] == 100


def test_parallel_requests_and_policy_changes_do_not_bypass_budget(tmp_path, monkeypatch):
    import asyncio
    configure(tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()

    async def complete(*_):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return "OK"

    monkeypatch.setattr(OllamaProvider, "complete", complete)
    with TestClient(app, base_url="http://127.0.0.1") as client, ThreadPoolExecutor(max_workers=1) as executor:
        record, headers = profile(client, {"token_budget": 50, "budget_period": "daily", "output_tokens": 50})
        body = {"model": record["slug"], "messages": [{"role": "user", "content": "ciao"}]}
        first = executor.submit(client.post, "/v1/chat/completions", headers=headers, json=body)
        assert started.wait(3)
        try:
            second = client.post("/v1/chat/completions", headers=headers, json=body)
            assert second.status_code == 429 and "Retry-After" in second.headers
            change = client.patch(f"/api/gateway-apis/{record['id']}", headers=ADMIN, json={"name": "Limited", "provider": "ollama", "model": "fixture", "reasoning_effort": "off", "limits": {"token_budget": 100, "budget_period": "monthly"}})
            assert change.status_code == 409
        finally:
            release.set()
        assert first.result().status_code == 200


def test_context_rejection_and_opt_in_summarization(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    calls = []

    async def complete(self, request, model):
        calls.append(request)
        assert count_request_tokens(request) + request.output_token_limit <= 512
        if "Summarize the supplied" in request.messages[0].text_content():
            assert request.tools == [] and request.tool_choice == "none"
            return "Older decision: preserve BLUE_MARKER."
        assert request.messages[0].text_content() == "KEEP_POLICY"
        assert request.messages[-1].text_content() == "LATEST_REQUEST"
        assert "BLUE_MARKER" in request.messages[1].text_content()
        return "final answer"

    monkeypatch.setattr(OllamaProvider, "complete", complete)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        record, headers = profile(client, {"context_tokens": 512, "output_tokens": 64})
        body = {"model": record["slug"], "messages": [{"role": "system", "content": "KEEP_POLICY"}, {"role": "user", "content": "historical fact " * 600}, {"role": "assistant", "content": "BLUE_MARKER"}, {"role": "user", "content": "LATEST_REQUEST"}]}
        rejected = client.post("/v1/chat/completions", headers=headers, json=body)
        assert rejected.status_code == 400 and rejected.json()["error"]["code"] == "context_limit_exceeded"
        assert not calls
        updated = client.patch(f"/api/gateway-apis/{record['id']}", headers=ADMIN, json={"name": "Limited", "provider": "ollama", "model": "fixture", "reasoning_effort": "off", "limits": {"context_tokens": 512, "output_tokens": 64, "context_mode": "summarize", "token_budget": 10000, "budget_period": "monthly"}})
        assert updated.status_code == 200
        compacted = client.post("/v1/chat/completions", headers=headers, json=body)
        assert compacted.status_code == 200, compacted.text
        assert int(compacted.headers["X-OmniProxy-Context-Compactions"]) > 0
        assert len(calls) > 1
        assert client.get("/v1/usage", headers=headers).json()["usage"]["used_tokens"] == compacted.json()["usage"]["total_tokens"]


def test_responses_persists_compacted_context_for_continuation(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    summaries = []

    async def complete(self, request, model):
        if "Summarize the supplied" in request.messages[0].text_content():
            summaries.append(True)
            return "Decision retained."
        return "OK"

    monkeypatch.setattr(OllamaProvider, "complete", complete)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        record, headers = profile(client, {"context_tokens": 512, "output_tokens": 64, "context_mode": "summarize"})
        first = client.post("/v1/responses", headers=headers, json={"model": record["slug"], "store": True, "input": [{"role": "user", "content": "old data " * 600}, {"role": "assistant", "content": "decision"}, {"role": "user", "content": "current"}]})
        assert first.status_code == 200 and int(first.headers["X-OmniProxy-Context-Compactions"]) > 0
        calls = len(summaries)
        followup = client.post("/v1/responses", headers=headers, json={"model": record["slug"], "previous_response_id": first.json()["id"], "input": "continue"})
        assert followup.status_code == 200
        assert followup.headers["X-OmniProxy-Context-Compactions"] == "0"
        assert len(summaries) == calls


def test_compaction_preserves_pending_tool_history_and_definitions(tmp_path, monkeypatch):
    from app.provider_broker import CodexBrokerClient
    configure(tmp_path, monkeypatch)

    async def catalog(_app):
        return [{"id": "codex", "connected": True, "models": [{"id": "fixture", "reasoning_efforts": ["low"]}]}]

    observed = []

    async def complete(self, request, *, model, reasoning_effort):
        if "Summarize the supplied" in request.messages[0].text_content():
            assert request.tools == [] and request.tool_choice == "none"
            return {"content": "Old facts summarized.", "tool_calls": []}
        observed.append(request)
        assert request.messages[0].text_content() == "PINNED_POLICY"
        assert request.messages[-2].tool_calls[0]["id"] == "call_current"
        assert request.messages[-1].tool_call_id == "call_current"
        assert request.tools[0]["function"]["name"] == "read_file"
        return {"content": "done", "tool_calls": []}

    monkeypatch.setattr("app.main._model_catalog", catalog)
    monkeypatch.setattr(CodexBrokerClient, "complete_with_tools", complete)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/api/gateway-apis", headers=ADMIN, json={"name": "Tool limits", "provider": "codex", "model": "fixture", "reasoning_effort": "low", "limits": {"context_tokens": 512, "output_tokens": 64, "context_mode": "summarize"}})
        assert response.status_code == 201
        headers = {"Authorization": f"Bearer {response.json()['api_key']}"}
        result = client.post("/v1/chat/completions", headers=headers, json={"model": response.json()["slug"], "messages": [
            {"role": "system", "content": "PINNED_POLICY"}, {"role": "user", "content": "old history " * 600}, {"role": "assistant", "content": "old response"},
            {"role": "user", "content": "current request"}, {"role": "assistant", "tool_calls": [{"id": "call_current", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]}, {"role": "tool", "tool_call_id": "call_current", "content": "current tool result"},
        ], "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]})
        assert result.status_code == 200 and len(observed) == 1


def test_input_output_and_context_can_be_independently_unlimited(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    calls = []

    async def complete(self, request, model):
        calls.append(request)
        assert request.output_token_limit == settings.ollama_context_tokens // 4
        return "unrestricted gateway output " * 20

    monkeypatch.setattr(OllamaProvider, "complete", complete)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        record, headers = profile(client, {"input_tokens": 8})
        body = {"model": record["slug"], "messages": [{"role": "user", "content": "input words " * 30}]}
        rejected = client.post("/v1/chat/completions", headers=headers, json=body)
        assert rejected.status_code == 400 and rejected.json()["error"]["code"] == "input_token_limit_exceeded"
        assert not calls
        updated = client.patch(f"/api/gateway-apis/{record['id']}", headers=ADMIN, json={"name": "Limited", "provider": "ollama", "model": "fixture", "reasoning_effort": "off", "limits": {"input_tokens": None, "output_tokens": None, "context_tokens": None, "budget_period": "unlimited", "token_budget": None}})
        assert updated.status_code == 200
        accepted = client.post("/v1/chat/completions", headers=headers, json=body)
        assert accepted.status_code == 200 and accepted.json()["choices"][0]["finish_reason"] == "stop"
        assert count_text_tokens(accepted.json()["choices"][0]["message"]["content"]) > 8


def test_input_limit_alone_can_trigger_summaries(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)

    async def complete(self, request, model):
        assert count_request_tokens(request) <= 256
        if "Summarize the supplied" in request.messages[0].text_content():
            return "Earlier facts retained."
        return "OK"

    monkeypatch.setattr(OllamaProvider, "complete", complete)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        record, headers = profile(client, {"input_tokens": 256, "output_tokens": 64, "context_mode": "summarize"})
        result = client.post("/v1/chat/completions", headers=headers, json={"model": record["slug"], "messages": [{"role": "user", "content": "old facts " * 600}, {"role": "assistant", "content": "old answer"}, {"role": "user", "content": "latest"}]})
        assert result.status_code == 200, result.text
        assert int(result.headers["X-OmniProxy-Context-Compactions"]) > 0


def test_limits_are_per_key_persisted_and_client_can_request_lower_output(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    observed = []

    async def complete(self, request, model):
        observed.append(request.output_token_limit)
        return "word " * 30

    monkeypatch.setattr(OllamaProvider, "complete", complete)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        limited, limited_headers = profile(client, {"input_tokens": 64, "output_tokens": 8})
        other = client.post("/api/gateway-apis", headers=ADMIN, json={"name": "Unlimited", "provider": "ollama", "model": "fixture", "reasoning_effort": "off", "limits": {}}).json()
        other_headers = {"Authorization": f"Bearer {other['api_key']}"}
        body = {"model": limited["slug"], "messages": [{"role": "user", "content": "hello"}]}
        small = client.post("/v1/chat/completions", headers=limited_headers, json={**body, "max_completion_tokens": 2})
        assert small.status_code == 200 and small.json()["usage"]["completion_tokens"] <= 2
        unrestricted = client.post("/v1/chat/completions", headers=other_headers, json={**body, "model": other["slug"]})
        assert unrestricted.status_code == 200 and unrestricted.json()["usage"]["completion_tokens"] > 8
        changed = client.patch(f"/api/gateway-apis/{limited['id']}", headers=ADMIN, json={"name": "Renamed", "provider": "ollama", "model": "fixture", "reasoning_effort": "off"})
        assert changed.status_code == 200
    # Reopen the application/database: an ordinary profile edit and a restart
    # must retain the policy, including null context and budget values.
    with TestClient(app, base_url="http://127.0.0.1") as client:
        usage = client.get("/v1/usage", headers=limited_headers).json()
        assert usage["limits"]["input_tokens"] == 64 and usage["limits"]["output_tokens"] == 8
        assert usage["limits"]["context_tokens"] is None
        response = client.post("/v1/chat/completions", headers=limited_headers, json=body)
        assert response.status_code == 200 and response.json()["usage"]["completion_tokens"] <= 8
    assert observed == [2, settings.ollama_context_tokens // 4, 8]
