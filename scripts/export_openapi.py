"""Export a deterministic public schema without starting the app or connecting to storage."""

import argparse
import json
from pathlib import Path

from app.main import app


def specification():
    schema = app.openapi()
    schema = json.loads(json.dumps(schema))
    schema["info"]["description"] = "OmniProxy contract 1, release candidate. Normative semantics: spec/README.md. Compatibility is a documented subset, not full vendor API equivalence."
    schema["x-omniproxy-contract-version"] = "1"
    schema["paths"] = {path: methods for path, methods in schema["paths"].items() if path.startswith(("/v1/", "/v1beta/", "/admin/v1/"))}
    components = schema.setdefault("components", {})
    components["securitySchemes"] = {
        "ApplicationBearer": {"type": "http", "scheme": "bearer", "description": "Local application key; cannot administer the gateway."},
        "HumanBearer": {"type": "http", "scheme": "bearer", "description": "Expiring human identity session; cannot call inference."},
        "AnthropicKey": {"type": "apiKey", "in": "header", "name": "x-api-key"},
        "GeminiKey": {"type": "apiKey", "in": "header", "name": "x-goog-api-key"},
    }
    components["schemas"]["GatewayError"] = {
        "type": "object", "required": ["error"], "properties": {"error": {
            "type": "object", "required": ["message", "type", "code", "param"], "properties": {
                "message": {"type": "string"}, "type": {"type": "string"},
                "code": {"type": ["string", "null"]}, "param": {"type": ["string", "null"]},
            },
        }},
    }
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method not in {"get", "post", "delete", "patch", "put"}:
                continue
            operation["security"] = [{"HumanBearer" if path.startswith("/admin/") else "ApplicationBearer": []}]
            if path == "/v1/messages":
                operation["security"].append({"AnthropicKey": []})
            if ":generateContent" in path or ":streamGenerateContent" in path:
                operation["security"].append({"GeminiKey": []})
            for code in ("400", "401", "403", "404", "409", "413", "422", "429", "503", "504"):
                operation["responses"].setdefault(code, {"description": "Gateway error; see stable code and protocol contract.", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GatewayError"}}}})
            # FastAPI's default validation response differs from our actual handler.
            if "422" in operation["responses"]:
                operation["responses"]["422"] = {"description": "Validation or domain error.", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GatewayError"}}}}
            for response in operation["responses"].values():
                response.setdefault("headers", {}).update({
                    "X-OmniProxy-Contract-Version": {"schema": {"type": "string", "const": "1"}},
                    "X-OmniProxy-Version": {"schema": {"type": "string"}},
                })
            if method == "post" and path in {"/v1/chat/completions", "/v1/responses", "/v1/messages"}:
                operation["responses"]["200"].setdefault("content", {})["text/event-stream"] = {"schema": {"type": "string"}, "description": "SSE lifecycle and terminal semantics are defined in spec/README.md."}
            if method == "post" and path == "/v1/audio/speech":
                operation["responses"]["200"]["content"] = {mime: {"schema": {"type": "string", "format": "binary"}} for mime in ("audio/wav", "audio/mpeg", "audio/flac", "audio/ogg", "audio/pcm", "audio/aac")}
    # Remove schemas reachable only through private legacy dashboard endpoints.
    pending = [schema["paths"]]
    needed = set()
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            ref = item.get("$ref", "")
            if ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[1]
                if name not in needed:
                    needed.add(name)
                    pending.append(components["schemas"][name])
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    components["schemas"] = {name: value for name, value in components["schemas"].items() if name in needed}
    return schema


def serialized():
    return json.dumps(specification(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("spec/openapi.json"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    value = serialized()
    if args.check:
        if args.output.read_text() != value:
            raise SystemExit("OpenAPI artifact differs: regenerate before releasing")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(value)


if __name__ == "__main__":
    main()
