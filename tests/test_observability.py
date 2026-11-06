import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from app.config import settings
from app.errors import GatewayError
from app.main import app
from app.providers.cloud_mock import ExternalReasoningMockProvider

KEY = "sk-local-telemetry-test-key-with-thirty-two-characters"


def test_real_otlp_export_excludes_prompt_output_headers_query_and_exception(tmp_path, monkeypatch):
    received = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            message = ExportTraceServiceRequest() if self.path == "/v1/traces" else ExportMetricsServiceRequest()
            message.ParseFromString(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, MessageToDict(message)))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(settings, "database_path", tmp_path / "telemetry.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setattr(settings, "otel_endpoint", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(settings, "otel_sample_rate", 1)
    monkeypatch.setattr(settings, "external_mock_latency_seconds", 0)
    marker = "PRIVATE_TELEMETRY_CANARY_39241"
    original = ExternalReasoningMockProvider.complete

    async def complete(self, payload, model):
        if "FAIL" in payload.messages[0].text_content():
            raise GatewayError(502, marker, code="fixture_error")
        return await original(self, payload, model)

    monkeypatch.setattr(ExternalReasoningMockProvider, "complete", complete)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            for content, stream in [(marker, False), (marker, True), ("FAIL " + marker, False)]:
                response = client.post("/v1/chat/completions?private=" + marker, headers={"Authorization": "Bearer " + KEY, "X-Private": marker}, json={"model": "reasoning-avanzato", "messages": [{"role": "user", "content": content}], "stream": stream})
                assert response.status_code == (502 if content.startswith("FAIL") else 200), response.text
            assert client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + KEY}, json={"model": marker, "messages": [{"role": "user", "content": marker}]}).status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    serialized = json.dumps(received)
    assert marker not in serialized and KEY not in serialized
    assert {path for path, _ in received} == {"/v1/traces", "/v1/metrics"}
    assert "gen_ai.operation.name" in serialized and "gen_ai.usage.input_tokens" in serialized
    assert "omniproxy.estimated_token.usage" in serialized and "gen_ai.client.operation.duration" in serialized
    assert "fixture_error" in serialized and "exception.message" not in serialized
