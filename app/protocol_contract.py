"""Public protocol discovery. Configuration is not a provider health probe."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import token_budget
from app.config import settings
from app.database import get_gateway_api_for_key
from app.routing import public_models

CONTRACT_VERSION = "1"
router = APIRouter()


class ContractHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def with_headers(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend([
                    (b"x-omniproxy-contract-version", CONTRACT_VERSION.encode()),
                    (b"x-omniproxy-version", settings.app_version.encode("ascii")),
                ])
            await send(message)
        public = scope["type"] == "http" and scope["path"].startswith(("/v1/", "/v1beta/", "/admin/v1/"))
        await self.app(scope, receive, with_headers if public else send)


@router.get("/v1/capabilities")
async def capabilities(request: Request):
    """Describe this key's contract without contacting providers or exposing credentials."""
    owner = int(request.state.api_key_id)
    binding = await get_gateway_api_for_key(owner)
    provider = binding.provider if binding else None
    managed = bool(binding and binding.mcp_tools)
    result = {
        "object": "gateway.capabilities",
        "contract_version": CONTRACT_VERSION,
        "product_version": settings.app_version,
        "availability": "configuration_only_not_health",
        "models": [binding.slug] if binding else [model["id"] for model in public_models()],
        "adapter": provider,
        "protocols": {
            "chat_completions": "subset",
            "responses": "subset",
            "anthropic_messages": "text_only",
            "gemini_generate_content": "text_only",
        },
        "tools": {
            "owner": "server" if managed else "client",
            "client_functions": not managed and provider in {"codex", "openai_compatible"},
            "parallel_client_functions": False,
            "server_mcp_configured": managed,
            "client_server_combination": False,
            "codex_images_with_client_functions": False,
        },
        "features": {
            "streaming": True,
            "stored_responses": True,
            "files": True,
            "citations": True,
            "repo_patch_jobs": bool(binding and provider in {"codex", "ollama", "openai_compatible"}),
            "structured_output": False,
            "image_input": "adapter_and_model_dependent",
            "audio_transcription_configured": bool(settings.audio_local_model or settings.audio_backend_url),
            "image_generation_configured": bool(settings.image_local_model or settings.image_backend_url),
            "speech": "local_synthetic",
        },
        "limits": (await token_budget.policy(owner)).model_dump(),
        "technical_limits": {
            "request_body_bytes": settings.max_request_body_bytes,
            "file_bytes": settings.file_max_bytes,
            "file_retention_days": settings.file_retention_days,
            "ollama_context_tokens": settings.ollama_context_tokens,
        },
        "usage_accounting": "gateway_estimate_not_provider_billing",
    }
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
