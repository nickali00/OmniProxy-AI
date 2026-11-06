"""External SDK examples and checks; no OmniProxy imports. Uses synthetic text only."""

import argparse
import importlib.metadata
import json
from pathlib import Path

from conformance.check import Probe


def verify(origin, key, model):
    from anthropic import Anthropic
    from google import genai
    from openai import OpenAI

    # Validate the destination before allowing SDKs to send the key.
    Probe(origin, key)
    result = []
    with OpenAI(api_key=key, base_url=origin + "/v1", max_retries=0, timeout=120) as client:
        chat = client.chat.completions.create(model=model, messages=[{"role": "user", "content": "Say OK."}], max_completion_tokens=64)
        if not chat.choices[0].message.content:
            raise RuntimeError("OpenAI chat result")
        response = client.responses.create(model=model, input="Say OK.", store=True, max_output_tokens=64)
        try:
            if not response.output_text or client.responses.retrieve(response.id).id != response.id:
                raise RuntimeError("OpenAI response storage")
            stream = client.responses.create(model=model, input="Say OK.", max_output_tokens=64, stream=True)
            with stream:
                events = list(stream)
            if not any(event.type == "response.output_text.delta" for event in events) or events[-1].type != "response.completed":
                raise RuntimeError("OpenAI response SSE")
        finally:
            client.responses.delete(response.id)
        result.append({"integration": "openai", "version": importlib.metadata.version("openai"), "checks": ["chat", "responses_store_retrieve_delete", "responses_sse"], "passed": True})
    with Anthropic(api_key=key, base_url=origin, max_retries=0, timeout=120) as client:
        message = client.messages.create(model=model, max_tokens=64, messages=[{"role": "user", "content": "Say OK."}])
        if not any(block.type == "text" and block.text for block in message.content):
            raise RuntimeError("Anthropic text")
        with client.messages.stream(model=model, max_tokens=64, messages=[{"role": "user", "content": "Say OK."}]) as stream:
            if not "".join(stream.text_stream):
                raise RuntimeError("Anthropic SSE")
        result.append({"integration": "anthropic", "version": importlib.metadata.version("anthropic"), "checks": ["messages", "messages_sse"], "passed": True})
    with genai.Client(api_key=key, http_options={"base_url": origin, "api_version": "v1beta"}) as client:
        response = client.models.generate_content(model=model, contents="Say OK.", config={"max_output_tokens": 64})
        if not response.text:
            raise RuntimeError("Google text")
        if not "".join(item.text or "" for item in client.models.generate_content_stream(model=model, contents="Say OK.", config={"max_output_tokens": 64})):
            raise RuntimeError("Google SSE")
        result.append({"integration": "google-genai", "version": importlib.metadata.version("google-genai"), "checks": ["generate_content", "generate_content_sse"], "passed": True})
    return {"contract_version": "1", "suite": "external-sdks", "passed": True, "integrations": result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.base_url.rstrip("/"), json.loads(args.key_file.read_text())["api_key"], args.model)
    except Exception:  # noqa: BLE001 -- SDK exceptions can include response bodies and credentials
        print('{"suite":"external-sdks","passed":false}')
        raise SystemExit(1) from None
    report = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(report)
    print(report)


if __name__ == "__main__":
    main()
