"""Contract 1 HTTP checks. Python standard library only; no OmniProxy imports.

Creates only synthetic responses/files/citations and removes its own resources.
Use a dedicated test key: inference consumes that key's configured budget.
"""

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Probe:
    def __init__(self, base_url, key):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("Use an HTTP(S) origin without credentials or path")
        self.base_url, self.key = base_url.rstrip("/"), key
        self.opener = urllib.request.build_opener(NoRedirect())
        self.results = []

    def call(self, method, path, data=None, *, key=None, raw=None, content_type=None):
        token = self.key if key is None else key
        headers = {"Authorization": "Bearer " + token} if token else {}
        if data is not None:
            raw = json.dumps(data).encode()
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.base_url + path, data=raw, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=300) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as response:
            return response.code, response.headers, response.read()

    def require(self, condition, name):
        if not condition:
            raise RuntimeError("Contract assertion failed: " + name)

    def check(self, name, function):
        started = time.perf_counter()
        try:
            function()
        except Exception:
            self.results.append({"name": name, "passed": False})
            raise
        self.results.append({"name": name, "passed": True, "duration_ms": round((time.perf_counter() - started) * 1000, 2)})

    def json_call(self, method, path, data=None, *, status=200, key=None):
        code, headers, body = self.call(method, path, data, key=key)
        self.require(code == status, path + ": status " + str(code))
        self.require(headers.get("X-OmniProxy-Contract-Version") == "1", "version header")
        return json.loads(body)


def verify(probe, model, other_key=None):
    response_ids, file_ids = [], []

    def authentication():
        body = probe.json_call("GET", "/v1/models", status=401, key="")
        probe.require(body["error"]["code"] == "invalid_api_key", "auth error")

    def discovery():
        body = probe.json_call("GET", "/v1/capabilities")
        probe.require(body["contract_version"] == "1" and model in body["models"], "discovery scope")
        models = probe.json_call("GET", "/v1/models")
        probe.require(models["object"] == "list" and model in [item["id"] for item in models["data"]], "models scope")
        usage = probe.json_call("GET", "/v1/usage")
        probe.require(usage["limits"] == body["limits"], "limits consistency")

    def validation():
        marker = "PRIVATE_CONFORMANCE_" + uuid.uuid4().hex
        body = probe.json_call("POST", "/v1/chat/completions", {"model": model, "messages": [{"role": "invalid", "content": marker}]}, status=400)
        probe.require(body["error"]["code"] == "validation_error" and marker not in json.dumps(body), "redacted validation")

    def chat():
        body = probe.json_call("POST", "/v1/chat/completions", {"model": model, "messages": [{"role": "user", "content": "Say only OK."}], "max_completion_tokens": 64})
        probe.require(body["object"] == "chat.completion" and bool(body["choices"][0]["message"]["content"]), "chat text")
        usage = body["usage"]
        probe.require(usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"], "usage total")

    def chat_stream():
        code, headers, raw = probe.call("POST", "/v1/chat/completions", {"model": model, "messages": [{"role": "user", "content": "Say only OK."}], "max_completion_tokens": 64, "stream": True, "stream_options": {"include_usage": True}})
        probe.require(code == 200 and headers.get_content_type() == "text/event-stream", "SSE transport")
        events = [line[6:] for line in raw.decode().splitlines() if line.startswith("data: ")]
        probe.require(bool(events) and events[-1] == "[DONE]", "SSE terminal")
        chunks = [json.loads(value) for value in events[:-1]]
        probe.require(any(item.get("choices", [{}])[0].get("delta", {}).get("content") for item in chunks if item.get("choices")), "SSE text")
        probe.require(any(item.get("usage") for item in chunks), "SSE usage")

    def responses():
        body = probe.json_call("POST", "/v1/responses", {"model": model, "input": "Say only BLUE.", "max_output_tokens": 64, "store": True})
        response_ids.append(body["id"])
        probe.require(body["object"] == "response" and body["status"] == "completed" and bool(body["output"]), "response result")
        probe.require(probe.json_call("GET", "/v1/responses/" + body["id"])["id"] == body["id"], "response retrieve")
        if other_key:
            probe.json_call("GET", "/v1/responses/" + body["id"], status=404, key=other_key)
        follow = probe.json_call("POST", "/v1/responses", {"model": model, "input": "Repeat the color.", "previous_response_id": body["id"], "max_output_tokens": 64, "store": True})
        response_ids.append(follow["id"])
        probe.require(follow["previous_response_id"] == body["id"], "response continuation")
        code, _, raw = probe.call("POST", "/v1/responses", {"model": model, "input": "Say only OK.", "max_output_tokens": 64, "stream": True})
        events = [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
        probe.require(code == 200 and any(event["type"] == "response.output_text.delta" for event in events) and events[-1]["type"] == "response.completed", "response SSE lifecycle")

    def files_and_citations():
        boundary = "omni" + uuid.uuid4().hex
        content = "😀 BLUE\n".encode()
        raw = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"contract.txt\"\r\nContent-Type: text/plain\r\n\r\n".encode() + content + f"\r\n--{boundary}--\r\n".encode())
        code, _, body = probe.call("POST", "/v1/files", raw=raw, content_type="multipart/form-data; boundary=" + boundary)
        probe.require(code == 201, "file upload")
        file_id = json.loads(body)["id"]
        file_ids.append(file_id)
        code, _, downloaded = probe.call("GET", f"/v1/files/{file_id}/content")
        probe.require(code == 200 and downloaded == content, "original file bytes")
        extracted = probe.json_call("GET", f"/v1/files/{file_id}/extraction")
        unit = extracted["units"][0]
        probe.require(extracted["source_sha256"] == hashlib.sha256(content).hexdigest() and extracted["offset_unit"] == "unicode_codepoints", "source hash and offsets")
        start = unit["text"].index("BLUE")
        value = {"file_id": file_id, "unit_id": unit["id"], "mode": "auto", "source_sha256": extracted["source_sha256"], "text_sha256": unit["sha256"], "start": start, "end": start + 4, "quote": "BLUE"}
        probe.require(probe.json_call("POST", "/v1/citations/verify", value)["valid"], "citation verify")
        citation = probe.json_call("POST", "/v1/citations", value, status=201)
        probe.require(probe.json_call("POST", "/v1/citations", value)["id"] == citation["id"], "citation idempotency")
        probe.require(probe.json_call("GET", "/v1/citations/" + citation["id"])["quote"] == "BLUE", "citation retrieve")
        probe.json_call("POST", "/v1/citations/verify", {**value, "quote": "PINK"}, status=422)
        if other_key:
            probe.json_call("GET", f"/v1/files/{file_id}/extraction", status=404, key=other_key)
            probe.json_call("GET", "/v1/citations/" + citation["id"], status=404, key=other_key)
        probe.json_call("DELETE", f"/v1/files/{file_id}")
        file_ids.remove(file_id)
        probe.json_call("GET", "/v1/citations/" + citation["id"], status=404)

    try:
        for name, operation in [("authentication", authentication), ("discovery_limits_models", discovery), ("redacted_validation", validation), ("chat_text_usage", chat), ("chat_sse_usage", chat_stream), ("responses_storage_continuation_sse_ownership", responses), ("files_unicode_citations_ownership_deletion", files_and_citations)]:
            probe.check(name, operation)
    finally:
        for path in [*("/v1/responses/" + item for item in response_ids), *("/v1/files/" + item for item in file_ids)]:
            code, _, _ = probe.call("DELETE", path)
            probe.require(code in {200, 204, 404}, "synthetic resource cleanup")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--key-file", type=Path, required=True, help='Private JSON file containing {"api_key": "..."}')
    parser.add_argument("--other-key-file", type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    probe = Probe(args.base_url, json.loads(args.key_file.read_text())["api_key"])
    other = json.loads(args.other_key_file.read_text())["api_key"] if args.other_key_file else None
    passed = False
    try:
        verify(probe, args.model, other)
        passed = True
    except Exception:  # noqa: BLE001 -- reporting boundary must redact transport exceptions
        # Do not serialize exception text: transports may include credentials/URLs/bodies.
        passed = False
    report = {"contract_version": "1", "suite": "core-http", "ownership_checked": bool(other), "passed": passed, "checks": probe.results}
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered)
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
