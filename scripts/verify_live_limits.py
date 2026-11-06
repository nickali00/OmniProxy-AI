"""Create and remove a temporary profile to verify a running gateway's limits."""

import argparse
import asyncio
import json
import uuid
from urllib.parse import urlsplit

import httpx


async def verify(url):
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    admin = {"Origin": origin, "X-OmniProxy-Request": "dashboard"}
    profile_id = None
    async with httpx.AsyncClient(base_url=url, timeout=180) as client:
        try:
            config = {"name": f"Limits smoke {uuid.uuid4().hex[:8]}", "provider": "ollama", "model": "qwen3.5:2b", "reasoning_effort": "off", "limits": {"input_tokens": 32, "output_tokens": 8, "token_budget": 500, "budget_period": "daily"}}
            created = await client.post("/api/gateway-apis", headers=admin, json=config)
            assert created.status_code == 201, (created.status_code, "profile creation failed")
            profile = created.json()
            profile_id = profile["id"]
            auth = {"Authorization": f"Bearer {profile['api_key']}"}
            body = {"model": profile["slug"], "messages": [{"role": "user", "content": "long input " * 100}]}
            rejected = await client.post("/v1/chat/completions", headers=auth, json=body)
            assert rejected.status_code == 400 and rejected.json()["error"]["code"] == "input_token_limit_exceeded"
            assert (await client.get("/v1/usage", headers=auth)).json()["usage"]["used_tokens"] == 0
            body["messages"][0]["content"] = "Say OK."
            normal = await client.post("/v1/chat/completions", headers=auth, json={**body, "max_completion_tokens": 64})
            assert normal.status_code == 200 and 0 < normal.json()["usage"]["completion_tokens"] <= 8
            config["limits"] = {"input_tokens": None, "output_tokens": None, "context_tokens": None, "context_mode": "reject", "token_budget": None, "budget_period": "unlimited"}
            changed = await client.patch(f"/api/gateway-apis/{profile_id}", headers=admin, json=config)
            assert changed.status_code == 200
            policy = (await client.get("/v1/usage", headers=auth)).json()["limits"]
            assert all(policy[key] is None for key in ("input_tokens", "output_tokens", "context_tokens", "token_budget"))
            unrestricted = await client.post("/v1/chat/completions", headers=auth, json={**body, "max_completion_tokens": 32})
            assert unrestricted.status_code == 200
            print(json.dumps({"scope": "running_gateway_temporary_profile", "input_cap": True, "output_cap": True, "remove_each_limit": True, "real_ollama": True, "passed": True}, indent=2))
        finally:
            if profile_id is not None:
                removed = await client.delete(f"/api/gateway-apis/{profile_id}", headers=admin)
                assert removed.status_code == 200, "Temporary profile cleanup failed"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    asyncio.run(verify(parser.parse_args().url))
