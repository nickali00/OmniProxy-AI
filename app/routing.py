from dataclasses import dataclass
from typing import Literal

from app.config import settings
from app.errors import GatewayError

ProviderName = Literal[
    "ollama",
    "codex",
    "gemini",
    "claude",
    "external_mock",
    "openai_compatible",
    "unresolved",
]


@dataclass(frozen=True)
class ModelRoute:
    requested_model: str
    provider: ProviderName
    resolved_model: str
    reasoning_effort: str = "auto"


def resolve_model_route(requested_model: str) -> ModelRoute:
    """
    Resolve public aliases to server-controlled providers and concrete models.

    Clients select only an alias. They cannot inject a provider URL, command,
    credential, or arbitrary cloud model name.
    """
    normalized = requested_model.strip().lower()

    if normalized in {"base", "local"}:
        return ModelRoute(
            requested_model=requested_model,
            provider="ollama",
            resolved_model=settings.ollama_model,
        )

    if normalized == "reasoning-avanzato":
        return ModelRoute(
            requested_model=requested_model,
            provider="external_mock",
            resolved_model="external-reasoning-placeholder",
        )

    if normalized == "upstream" and settings.upstream_base_url and settings.upstream_model:
        return ModelRoute(
            requested_model=requested_model,
            provider="openai_compatible",
            resolved_model=settings.upstream_model,
        )

    if normalized.startswith("upstream:"):
        from app.deployments import configured
        if normalized.removeprefix("upstream:") in configured():
            return ModelRoute(requested_model, "openai_compatible", normalized)

    raise GatewayError(
        404,
        (
            f"The model '{requested_model}' does not exist. "
            "Available models: base, local, reasoning-avanzato."
        ),
        error_type="invalid_request_error",
        code="model_not_found",
        param="model",
    )


def public_models() -> list[dict[str, object]]:
    models = [
        {
            "id": "base",
            "object": "model",
            "created": 0,
            "owned_by": "omni-proxy-local",
        },
        {
            "id": "local",
            "object": "model",
            "created": 0,
            "owned_by": "omni-proxy-local",
        },
        {
            "id": "reasoning-avanzato",
            "object": "model",
            "created": 0,
            "owned_by": "omni-proxy-external",
        },
    ]
    if settings.upstream_base_url and settings.upstream_model:
        models.append({"id": "upstream", "object": "model", "created": 0, "owned_by": "omni-proxy-upstream"})
    from app.deployments import configured
    models.extend({"id": "upstream:" + identifier, "object": "model", "created": 0, "owned_by": "omni-proxy-upstream"} for identifier in configured())
    return models
