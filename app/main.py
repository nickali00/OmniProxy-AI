from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from difflib import SequenceMatcher
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.background import BackgroundTask

from app.auth import ApiKeyAuthMiddleware
from app.identity import AdministrativeAuthMiddleware, check_auth_mode
from app.team_api import router as team_router
from app.job_api import router as job_router
from app.file_uploads import init_files, resolve_chat_files, router as file_router
from app.audio_api import router as audio_router
from app.speech_api import router as speech_router
from app.image_api import router as image_router
from app.job_store import init_jobs
from app.job_tests import init_tests, router as job_tests_router
from app.shared_capacity import init_capacity
from app.citations import init_citations, router as citation_router
from app.job_profiles import router as job_profile_router
from app.protocol_contract import ContractHeadersMiddleware, router as contract_router
from app.api_groups import router as api_groups_router
from app.security import SecurityBoundaryMiddleware, harden_storage_permissions, maintenance_authorization
from app.network_security import enforce_upstream_transport
from app.jobs import JobRunner
from app.inference_limits import InferenceLimits
from app import deployments, observability, token_budget
from app.anthropic_api import (
    AnthropicRequest,
    from_chat_response as anthropic_from_chat_response,
    stream_chat_response as anthropic_stream_chat_response,
    to_chat_request as anthropic_to_chat_request,
)
from app.capabilities import (
    CapabilityContext,
    apply_capability_context,
    complete_with_mcp,
    load_capability_context,
)
from app.config import settings
from app.dashboard_security import require_dashboard_mutation
from app.database import (
    McpServerRecord,
    add_build_message,
    build_project_context,
    claim_next_build_phase,
    complete_build_phase_for_message,
    create_build_project,
    create_gateway_api,
    create_mcp_server,
    create_skill,
    delete_build_project,
    delete_gateway_api,
    delete_mcp_server,
    delete_stored_response,
    delete_skill,
    ensure_bootstrap_api_key,
    get_build_project,
    get_gateway_api,
    get_gateway_api_for_key,
    get_stored_response,
    get_mcp_server,
    get_usage_dashboard,
    init_database,
    list_build_artifacts,
    list_build_messages,
    list_build_phases,
    list_build_project_file_contents,
    list_build_project_files,
    list_build_projects,
    list_gateway_apis,
    list_mcp_servers,
    list_skills,
    mark_build_message_changes_applied,
    ping_database,
    record_usage,
    replace_build_artifacts,
    replace_build_phases,
    replace_build_project_files,
    save_mcp_server_discovery,
    save_stored_response,
    set_build_phase_builder_result,
    toggle_api_key_status,
    update_build_project,
    update_gateway_api,
    update_mcp_server,
    update_skill,
)
from app.errors import GatewayError, openai_error_body
from app.gemini_api import (
    GeminiRequest,
    from_chat_response as gemini_from_chat_response,
    stream_chat_response as gemini_stream_chat_response,
    to_chat_request as gemini_to_chat_request,
)
from app.mcp_client import McpClientError, McpGatewayClient, normalize_mcp_url
from app.provider_broker import (
    AntigravityBrokerClient,
    ClaudeBrokerClient,
    CodexBrokerClient,
    ProviderBrokerError,
)
from app.provider_status import dashboard_providers
from app.provider_vault import ProviderVault, ProviderVaultError
from app.providers.cli_broker import CliBrokerProvider
from app.providers.base import TextProvider
from app.providers.cloud_mock import ExternalReasoningMockProvider
from app.providers.ollama import OllamaProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.routing import ModelRoute, public_models, resolve_model_route
from app.request_lifecycle import ClientDisconnectMiddleware, RequestLimitsMiddleware, await_connected
from app.responses_api import (
    ResponsesRequest,
    from_chat_input,
    from_chat_response,
    input_items,
    stream_chat_response,
    to_chat_request,
)
from app.schemas import (
    BuildChatRequest,
    BuildFileSnapshot,
    BuildFileSync,
    BuildHandoffRequest,
    BuildModelConfig,
    BuildPlanRequest,
    BuildProjectConfig,
    ChatCompletionRequest,
    GatewayApiConfig,
    McpServerConfig,
    ProviderAuthCode,
    SkillConfig,
)
from app.token_counter import count_message_tokens, count_request_tokens, count_text_tokens

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

BUILD_FILE_LIMIT = 2_000
BUILD_FILE_BYTES_LIMIT = 1_048_576
BUILD_TOTAL_BYTES_LIMIT = 50_000_000
BUILD_EXCLUDED_DIRECTORIES = {
    ".git",
    ".idea",
    ".next",
    ".nuxt",
    ".pytest_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.authentication_failures = None
    if settings.require_https and not (settings.dashboard_auth_required or settings.admin_auth_required):
        raise RuntimeError("HTTPS profile requires authenticated dashboard access")
    await init_database()
    harden_storage_permissions()
    await check_auth_mode()
    await ensure_bootstrap_api_key()
    await init_jobs()
    await init_tests()
    await init_files()
    await init_capacity()
    await init_citations()

    timeout = httpx.Timeout(
        connect=10.0,
        read=settings.ollama_request_timeout_seconds,
        write=30.0,
        pool=10.0,
    )
    ollama_urls = dict.fromkeys(
        [
            settings.ollama_base_url.rstrip("/"),
            "http://ollama:11434",
            "http://host.docker.internal:11434",
        ]
    )
    app.state.ollama_http_clients = [
        httpx.AsyncClient(base_url=url, timeout=timeout) for url in ollama_urls
    ]
    broker_timeout = httpx.Timeout(settings.provider_broker_timeout_seconds)
    app.state.codex_broker_http = httpx.AsyncClient(
        base_url=settings.codex_broker_url.rstrip("/"),
        timeout=broker_timeout,
    )
    app.state.gemini_broker_http = httpx.AsyncClient(
        base_url=settings.gemini_broker_url.rstrip("/"),
        timeout=broker_timeout,
    )
    app.state.claude_broker_http = httpx.AsyncClient(
        base_url=settings.claude_broker_url.rstrip("/"),
        timeout=broker_timeout,
    )
    app.state.maintenance_http = httpx.AsyncClient(
        base_url=settings.maintenance_runner_url.rstrip("/"),
        timeout=httpx.Timeout(10.0),
        event_hooks={"request": [maintenance_authorization]},
        trust_env=False,
    )
    app.state.build_activities = {}
    app.state.file_processing_slots = asyncio.Semaphore(2)
    app.state.audio_processing_slots = asyncio.Semaphore(1)
    app.state.speech_processing_slots = asyncio.Semaphore(2)
    app.state.image_processing_slots = asyncio.Semaphore(1)
    app.state.api_request_slots = asyncio.Semaphore(settings.max_concurrent_api_requests)
    app.state.telemetry = observability.configure()
    app.state.job_runner = JobRunner(lambda profile, payload: _job_complete(app, profile, payload))
    app.state.job_runner.start()
    try:
        yield
    finally:
        await app.state.job_runner.stop()
        if app.state.telemetry:
            await asyncio.to_thread(app.state.telemetry.shutdown)
        await asyncio.gather(
            *[client.aclose() for client in app.state.ollama_http_clients],
            app.state.codex_broker_http.aclose(),
            app.state.gemini_broker_http.aclose(),
            app.state.claude_broker_http.aclose(),
            app.state.maintenance_http.aclose(),
        )


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)
app.include_router(job_router)
app.include_router(job_tests_router)
app.include_router(file_router)
app.include_router(audio_router)
app.include_router(speech_router)
app.include_router(image_router)
app.include_router(team_router)
app.include_router(citation_router)
app.include_router(job_profile_router)
app.include_router(contract_router)
app.include_router(api_groups_router)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

# SICUREZZA DASHBOARD:
# oltre al binding Docker su 127.0.0.1, rifiutiamo Host arbitrari per impedire
# DNS rebinding contro gli endpoint locali di collegamento provider.
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[
        host.strip()
        for host in settings.dashboard_allowed_hosts.split(",")
        if host.strip()
    ],
)

# AUTENTICAZIONE:
# il middleware protegge l'intero namespace /v1 e risolve la Bearer key nel DB.
# Negli endpoint arriva soltanto l'id interno della chiave, mai il segreto.
app.add_middleware(RequestLimitsMiddleware)
app.add_middleware(ApiKeyAuthMiddleware)
app.add_middleware(AdministrativeAuthMiddleware)


@app.middleware("http")
async def dashboard_security_headers(request: Request, call_next):
    if (
        not settings.build_enabled
        and (
            request.url.path == "/api/build"
            or request.url.path.startswith("/api/build/")
        )
    ):
        return JSONResponse(
            status_code=404,
            content={
                "error": "not_found",
                "message": "Endpoint non disponibile.",
            },
            headers={"Cache-Control": "no-store"},
        )
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith(
        (
            "/static/",
            "/api/providers/",
            "/api/models/",
            "/api/gateway-apis",
            "/api/keys/",
            "/api/usage",
            "/api/skills",
            "/api/mcp/",
            "/api/build/",
            "/api/workspaces",
        )
    ):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
    if request.url.path.startswith(
        (
            "/static/",
            "/api/providers/",
            "/api/models/",
            "/api/gateway-apis",
            "/api/keys/",
            "/api/usage",
            "/api/skills",
            "/api/mcp/",
            "/api/build/",
            "/api/workspaces",
        )
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


app.add_middleware(ClientDisconnectMiddleware)
app.add_middleware(observability.TelemetryMiddleware)
app.add_middleware(ContractHeadersMiddleware)
app.add_middleware(SecurityBoundaryMiddleware)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[host.strip() for host in settings.dashboard_allowed_hosts.split(",") if host.strip()])


@app.exception_handler(GatewayError)
async def gateway_error_handler(
    request: Request,
    exc: GatewayError,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content=openai_error_body(
            exc.message,
            error_type=exc.error_type,
            code=exc.code,
            param=exc.param,
        ),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    if request.url.path.startswith(
        (
            "/api/providers/",
            "/api/models/",
            "/api/gateway-apis",
            "/api/skills",
            "/api/mcp/",
            "/api/build/",
        )
    ):
        # Gli errori Pydantic includono normalmente l'input ricevuto. Nel
        # namespace OAuth non deve mai tornare al browser un codice monouso,
        # nemmeno quando è malformato.
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_provider_request",
                "message": "Richiesta provider non valida.",
            },
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        status_code=400,
        content=openai_error_body(
            "Richiesta non valida: controlla formato, capacità e limiti.",
            error_type="invalid_request_error",
            code="validation_error",
        ),
    )


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
async def admin_login(request: Request):
    return templates.TemplateResponse(request=request, name="admin-login.html", context={}, headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; form-action 'self'", "Referrer-Policy": "no-referrer"})


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def team_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="team.html", context={}, headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; form-action 'self'", "Referrer-Policy": "no-referrer"})


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(
    request: Request,
) -> Response:
    providers = dashboard_providers()
    connected_count = sum(
        provider["state"] in {"connected", "detected"} for provider in providers
    )
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "providers": providers,
            "connected_count": connected_count,
            "total_providers": len(providers),
            "build_enabled": settings.build_enabled,
        },
    )


@app.get("/readyz")
async def readiness(request: Request) -> JSONResponse:
    database_ready = await ping_database()
    ollama = await _discover_ollama(request.app, timeout=2.0)
    broker_results = await asyncio.gather(
        _broker_available(request.app.state.codex_broker_http),
        _broker_available(request.app.state.gemini_broker_http),
        _broker_available(request.app.state.claude_broker_http),
    )
    status_code = 200 if database_ready else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ready" if status_code == 200 else "not_ready",
            "database": database_ready,
            "providers": {
                "ollama": ollama is not None,
                "codex": broker_results[0],
                "gemini": broker_results[1],
                "claude": broker_results[2],
            },
        },
    )


@app.get("/api/providers/ollama/status", include_in_schema=False)
async def ollama_provider_status(request: Request) -> JSONResponse:
    discovered = await _discover_ollama(request.app, timeout=2.0)
    if discovered is None:
        return JSONResponse(
            content={
                "provider": "ollama",
                "installed": False,
                "connected": False,
                "auth_method": "none",
                "models": [],
            },
            headers={"Cache-Control": "no-store"},
        )
    _, payload = discovered
    models = [
        item.get("name")
        for item in payload.get("models", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    return JSONResponse(
        content={
            "provider": "ollama",
            "installed": True,
            "connected": True,
            "auth_method": "container_api",
            "models": models,
            "configured_model": settings.ollama_model,
            "configured_model_available": any(
                name == settings.ollama_model
                or name.startswith(f"{settings.ollama_model}:")
                for name in models
            ),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/providers/codex/status", include_in_schema=False)
async def codex_provider_status(request: Request) -> JSONResponse:
    try:
        payload = await CodexBrokerClient(
            request.app.state.codex_broker_http
        ).status()
    except ProviderBrokerError as exc:
        return _provider_error_response(exc)
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})


async def _latest_codex_cli_version() -> str | None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get("https://registry.npmjs.org/@openai%2fcodex/latest")
            response.raise_for_status()
            version = response.json().get("version")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        return None
    return version


@app.get("/api/providers/codex/update", include_in_schema=False)
async def codex_update_status(request: Request) -> JSONResponse:
    try:
        installed = (await CodexBrokerClient(request.app.state.codex_broker_http).status()).get("cli_version")
    except ProviderBrokerError:
        installed = None
    latest = await _latest_codex_cli_version()
    installed_match = re.match(r"^(\d+)\.(\d+)\.(\d+)", installed or "")
    installed_core = tuple(map(int, installed_match.groups())) if installed_match else None
    latest_core = tuple(map(int, latest.split("."))) if latest else None
    update_available = bool(
        installed_core and latest_core and
        (latest_core > installed_core or (latest_core == installed_core and latest != installed))
    )
    return JSONResponse(content={"installed": installed, "latest": latest,
        "update_available": update_available},
        headers={"Cache-Control": "no-store"})


@app.post("/api/providers/codex/update", include_in_schema=False,
          dependencies=[Depends(require_dashboard_mutation)])
async def start_codex_update(request: Request) -> JSONResponse:
    version = await _latest_codex_cli_version()
    if version is None:
        return _build_error(503, "version_check_failed", "Versione Codex non verificabile dal registro npm.")
    try:
        response = await request.app.state.maintenance_http.post(
            "/v1/codex/update", json={"version": version})
        response.raise_for_status()
        job = response.json()
    except (httpx.HTTPError, ValueError):
        return _build_error(503, "maintenance_runner_unavailable", "Il runner non ha avviato l’aggiornamento Codex.")
    return JSONResponse(status_code=202, content=job, headers={"Cache-Control": "no-store"})


@app.get("/api/providers/codex/update/jobs/{job_id}", include_in_schema=False)
async def codex_update_job(job_id: uuid.UUID, request: Request) -> JSONResponse:
    try:
        response = await request.app.state.maintenance_http.get(f"/v1/jobs/{job_id}")
        response.raise_for_status()
        job = response.json()
    except (httpx.HTTPError, ValueError):
        return _build_error(503, "maintenance_runner_unavailable", "Stato aggiornamento Codex non disponibile.")
    return JSONResponse(content=job, headers={"Cache-Control": "no-store"})


@app.post(
    "/api/providers/codex/auth/start",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def start_codex_auth(request: Request) -> JSONResponse:
    try:
        payload = await CodexBrokerClient(
            request.app.state.codex_broker_http
        ).start_auth()
    except ProviderBrokerError as exc:
        return _provider_error_response(exc)
    return JSONResponse(
        status_code=201 if payload.get("auth_url") else 200,
        content=payload,
        headers={"Cache-Control": "no-store"},
    )


@app.delete(
    "/api/providers/codex/auth/{attempt_id}",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def cancel_codex_auth(
    attempt_id: str,
    request: Request,
) -> JSONResponse:
    try:
        payload = await CodexBrokerClient(
            request.app.state.codex_broker_http
        ).cancel_auth(attempt_id)
    except ProviderBrokerError as exc:
        return _provider_error_response(exc)
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})


@app.get("/api/providers/gemini/status", include_in_schema=False)
async def gemini_provider_status(request: Request) -> JSONResponse:
    try:
        payload = await AntigravityBrokerClient(
            request.app.state.gemini_broker_http
        ).status()
    except ProviderBrokerError as exc:
        return _provider_error_response(exc)
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})


@app.post(
    "/api/providers/gemini/auth/start",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def start_gemini_auth(request: Request) -> JSONResponse:
    try:
        payload = await AntigravityBrokerClient(
            request.app.state.gemini_broker_http
        ).start_auth()
    except ProviderBrokerError as exc:
        return _provider_error_response(exc)
    return JSONResponse(
        status_code=201 if payload.get("auth_url") else 200,
        content=payload,
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/providers/gemini/auth/{attempt_id}/code",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def submit_gemini_auth_code(
    attempt_id: uuid.UUID,
    payload: ProviderAuthCode,
    request: Request,
) -> JSONResponse:
    invalid = _invalid_auth_code_response(payload.code)
    if invalid is not None:
        return invalid
    try:
        result = await AntigravityBrokerClient(
            request.app.state.gemini_broker_http
        ).submit_code(
            str(attempt_id),
            payload.code,
        )
    except ProviderBrokerError as exc:
        return _provider_error_response(exc)
    return JSONResponse(
        status_code=202,
        content=result,
        headers={"Cache-Control": "no-store"},
    )


@app.delete(
    "/api/providers/gemini/auth/{attempt_id}",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def cancel_gemini_auth(
    attempt_id: uuid.UUID,
    request: Request,
) -> JSONResponse:
    try:
        payload = await AntigravityBrokerClient(
            request.app.state.gemini_broker_http
        ).cancel_auth(str(attempt_id))
    except ProviderBrokerError as exc:
        return _provider_error_response(exc)
    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "no-store"},
    )


@app.delete(
    "/api/providers/gemini/connection",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def disconnect_gemini(request: Request) -> JSONResponse:
    try:
        payload = await AntigravityBrokerClient(
            request.app.state.gemini_broker_http
        ).disconnect()
    except ProviderBrokerError as exc:
        return _provider_error_response(exc)
    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/providers/claude/status", include_in_schema=False)
async def claude_provider_status(request: Request) -> JSONResponse:
    try:
        payload = await ClaudeBrokerClient(
            request.app.state.claude_broker_http
        ).status()
    except ProviderBrokerError as exc:
        return _provider_error_response(exc)
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})


@app.get("/api/providers/quotas", include_in_schema=False)
async def provider_quotas(request: Request) -> JSONResponse:
    """Aggregate provider limits without exposing account credentials."""
    ollama_discovery, codex, gemini, claude = await asyncio.gather(
        _discover_ollama(request.app, timeout=2.0),
        CodexBrokerClient(request.app.state.codex_broker_http).quota(),
        AntigravityBrokerClient(request.app.state.gemini_broker_http).quota(),
        ClaudeBrokerClient(request.app.state.claude_broker_http).quota(),
        return_exceptions=True,
    )

    providers: list[dict[str, object]] = [
        {
            "provider": "ollama",
            "connected": (
                not isinstance(ollama_discovery, BaseException)
                and ollama_discovery is not None
            ),
            "available": (
                not isinstance(ollama_discovery, BaseException)
                and ollama_discovery is not None
            ),
            "unlimited": (
                not isinstance(ollama_discovery, BaseException)
                and ollama_discovery is not None
            ),
            "source": "local_container",
            "reason": (
                None
                if not isinstance(ollama_discovery, BaseException)
                and ollama_discovery is not None
                else "not_connected"
            ),
            "remaining_percent": (
                100
                if not isinstance(ollama_discovery, BaseException)
                and ollama_discovery is not None
                else None
            ),
            "windows": [],
        }
    ]
    for provider_id, result in zip(
        ("codex", "gemini", "claude"),
        (codex, gemini, claude),
        strict=True,
    ):
        if isinstance(result, BaseException):
            providers.append(
                {
                    "provider": provider_id,
                    "connected": False,
                    "available": False,
                    "unlimited": False,
                    "source": "provider_broker",
                    "reason": "temporarily_unavailable",
                    "remaining_percent": None,
                    "windows": [],
                }
            )
        else:
            providers.append(result)
    return JSONResponse(
        content={"providers": providers},
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/providers/claude/auth/start",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def start_claude_auth(request: Request) -> JSONResponse:
    # ROUTING AUTENTICAZIONE PROVIDER:
    # il gateway non esegue OAuth e non conserva token. Delega al sidecar
    # Claude, valida nuovamente l'URL e restituisce alla UI solo il link
    # monouso da aprire nel browser ufficiale.
    try:
        payload = await ClaudeBrokerClient(
            request.app.state.claude_broker_http
        ).start_auth()
    except ProviderBrokerError as exc:
        return _provider_error_response(exc)
    return JSONResponse(
        status_code=201 if payload.get("auth_url") else 200,
        content=payload,
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/providers/claude/auth/{attempt_id}/code",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def submit_claude_auth_code(
    attempt_id: uuid.UUID,
    payload: ProviderAuthCode,
    request: Request,
) -> JSONResponse:
    # Il codice OAuth è monouso: passa in memoria e viene subito inoltrato al
    # processo Claude Code. Non viene scritto in SQLite, file o log.
    invalid = _invalid_auth_code_response(payload.code)
    if invalid is not None:
        return invalid
    try:
        result = await ClaudeBrokerClient(
            request.app.state.claude_broker_http
        ).submit_code(str(attempt_id), payload.code)
    except ProviderBrokerError as exc:
        return _provider_error_response(exc)
    return JSONResponse(
        status_code=202,
        content=result,
        headers={"Cache-Control": "no-store"},
    )


@app.delete(
    "/api/providers/claude/auth/{attempt_id}",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def cancel_claude_auth(
    attempt_id: uuid.UUID,
    request: Request,
) -> JSONResponse:
    try:
        payload = await ClaudeBrokerClient(
            request.app.state.claude_broker_http
        ).cancel_auth(str(attempt_id))
    except ProviderBrokerError as exc:
        return _provider_error_response(exc)
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})


@app.get("/api/models/catalog", include_in_schema=False)
async def model_catalog(request: Request) -> JSONResponse:
    return JSONResponse(
        content={"providers": await _model_catalog(request.app)},
        headers={"Cache-Control": "no-store"},
    )


@app.patch(
    "/api/keys/{key_id}/toggle",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def api_key_toggle(key_id: int) -> JSONResponse:
    record = await toggle_api_key_status(key_id)
    if record is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "api_key_not_found",
                "message": "API key locale non trovata o revocata.",
            },
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        content=record,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/skills", include_in_schema=False)
async def skill_list() -> JSONResponse:
    records = await list_skills()
    return JSONResponse(
        content={"data": [record.as_dict() for record in records]},
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/skills",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def skill_create(payload: SkillConfig) -> JSONResponse:
    try:
        record = await create_skill(
            name=payload.name,
            description=payload.description,
            instructions=payload.instructions,
            enabled=payload.enabled,
        )
    except ValueError as exc:
        return _build_error(409, "skill_conflict", str(exc))
    return JSONResponse(
        status_code=201,
        content=record.as_dict(),
        headers={"Cache-Control": "no-store"},
    )


@app.patch(
    "/api/skills/{skill_id}",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def skill_update(skill_id: int, payload: SkillConfig) -> JSONResponse:
    try:
        record = await update_skill(
            skill_id,
            name=payload.name,
            description=payload.description,
            instructions=payload.instructions,
            enabled=payload.enabled,
        )
    except ValueError as exc:
        return _build_error(409, "skill_conflict", str(exc))
    if record is None:
        return _build_error(404, "skill_not_found", "Skill non trovata.")
    return JSONResponse(
        content=record.as_dict(),
        headers={"Cache-Control": "no-store"},
    )


@app.delete(
    "/api/skills/{skill_id}",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def skill_delete(skill_id: int) -> JSONResponse:
    if not await delete_skill(skill_id):
        return _build_error(404, "skill_not_found", "Skill non trovata.")
    return JSONResponse(
        content={"status": "deleted"},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/mcp/servers", include_in_schema=False)
async def mcp_server_list() -> JSONResponse:
    records = await list_mcp_servers()
    return JSONResponse(
        content={"data": [record.as_dict() for record in records]},
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/mcp/servers",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def mcp_server_create(payload: McpServerConfig) -> JSONResponse:
    try:
        url = normalize_mcp_url(payload.url)
    except ValueError as exc:
        return _build_error(400, "invalid_mcp_url", str(exc))
    try:
        if payload.auth_type == "bearer" and not payload.bearer_token:
            return _build_error(
                400,
                "mcp_token_required",
                "Inserisci il token Bearer del server MCP.",
            )
        ciphertext = (
            ProviderVault().encrypt_json(
                {"bearer_token": payload.bearer_token}
            )
            if payload.auth_type == "bearer"
            else None
        )
        record = await create_mcp_server(
            name=payload.name,
            url=url,
            auth_type=payload.auth_type,
            auth_ciphertext=ciphertext,
            enabled=payload.enabled,
        )
    except ProviderVaultError:
        return _build_error(
            503,
            "mcp_vault_error",
            "Il vault delle credenziali MCP non è disponibile.",
        )
    except ValueError as exc:
        return _build_error(409, "mcp_server_conflict", str(exc))
    if record.enabled:
        record = await _refresh_mcp_server(record)
    return JSONResponse(
        status_code=201,
        content=record.as_dict(),
        headers={"Cache-Control": "no-store"},
    )


@app.patch(
    "/api/mcp/servers/{server_id}",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def mcp_server_update(
    server_id: int,
    payload: McpServerConfig,
) -> JSONResponse:
    existing = await get_mcp_server(server_id)
    if existing is None:
        return _build_error(
            404,
            "mcp_server_not_found",
            "Server MCP non trovato.",
        )
    try:
        url = normalize_mcp_url(payload.url)
    except ValueError as exc:
        return _build_error(400, "invalid_mcp_url", str(exc))
    try:
        if payload.auth_type == "bearer":
            if payload.bearer_token:
                ciphertext = ProviderVault().encrypt_json(
                    {"bearer_token": payload.bearer_token}
                )
            elif existing.auth_type == "bearer" and existing.auth_ciphertext:
                ciphertext = existing.auth_ciphertext
            else:
                return _build_error(
                    400,
                    "mcp_token_required",
                    "Inserisci il token Bearer del server MCP.",
                )
        else:
            ciphertext = None
        record = await update_mcp_server(
            server_id,
            name=payload.name,
            url=url,
            auth_type=payload.auth_type,
            auth_ciphertext=ciphertext,
            enabled=payload.enabled,
        )
    except ProviderVaultError:
        return _build_error(
            503,
            "mcp_vault_error",
            "Il vault delle credenziali MCP non è disponibile.",
        )
    except ValueError as exc:
        return _build_error(409, "mcp_server_conflict", str(exc))
    if record is None:
        return _build_error(
            404,
            "mcp_server_not_found",
            "Server MCP non trovato.",
        )
    if record.enabled:
        record = await _refresh_mcp_server(record)
    return JSONResponse(
        content=record.as_dict(),
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/mcp/servers/{server_id}/refresh",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def mcp_server_refresh(server_id: int) -> JSONResponse:
    record = await get_mcp_server(server_id)
    if record is None:
        return _build_error(
            404,
            "mcp_server_not_found",
            "Server MCP non trovato.",
        )
    if not record.enabled:
        return _build_error(
            409,
            "mcp_server_disabled",
            "Riattiva il server MCP prima di verificarlo.",
        )
    record = await _refresh_mcp_server(record)
    return JSONResponse(
        content=record.as_dict(),
        headers={"Cache-Control": "no-store"},
    )


@app.delete(
    "/api/mcp/servers/{server_id}",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def mcp_server_delete(server_id: int) -> JSONResponse:
    if not await delete_mcp_server(server_id):
        return _build_error(
            404,
            "mcp_server_not_found",
            "Server MCP non trovato.",
        )
    return JSONResponse(
        content={"status": "deleted"},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/gateway-apis", include_in_schema=False)
async def gateway_api_list() -> JSONResponse:
    records = await list_gateway_apis()
    budgets = await asyncio.gather(*(token_budget.status(record.api_key_id) for record in records))
    return JSONResponse(
        content={"data": [{**record.as_dict(), "token_usage": budget} for record, budget in zip(records, budgets, strict=True)]},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/usage", include_in_schema=False)
async def usage_dashboard(
    period: str = "7d",
    api_key_id: int | None = None,
    provider: str | None = None,
    limit: int = 50,
) -> JSONResponse:
    period_modifiers = {
        "24h": "-1 day",
        "7d": "-7 days",
        "30d": "-30 days",
        "all": None,
    }
    if period not in period_modifiers:
        return _build_error(
            400,
            "invalid_usage_period",
            "Periodo consumi non valido.",
        )
    if api_key_id is not None and api_key_id <= 0:
        return _build_error(
            400,
            "invalid_usage_api",
            "Filtro API non valido.",
        )
    if provider is not None:
        provider = provider.strip().lower()
        if not provider or len(provider) > 64:
            return _build_error(
                400,
                "invalid_usage_provider",
                "Filtro provider non valido.",
            )
    result = await get_usage_dashboard(
        since_modifier=period_modifiers[period],
        api_key_id=api_key_id,
        provider=provider,
        limit=limit,
    )
    return JSONResponse(
        content={"period": period, **result},
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/gateway-apis",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def gateway_api_create(
    payload: GatewayApiConfig,
    request: Request,
) -> JSONResponse:
    invalid = await _validate_gateway_api_config(request.app, payload)
    if invalid is not None:
        return invalid
    try:
        record, secret = await create_gateway_api(
            name=payload.name,
            provider=payload.provider,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
            skill_ids=payload.skill_ids,
            mcp_tools=payload.flattened_mcp_tools,
            limits=payload.limits.model_dump() if payload.limits is not None else None,
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=409,
            content={"error": "gateway_api_conflict", "message": str(exc)},
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        status_code=201,
        content={**record.as_dict(), "api_key": secret},
        headers={"Cache-Control": "no-store"},
    )


@app.patch(
    "/api/gateway-apis/{api_id}",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def gateway_api_update(
    api_id: int,
    payload: GatewayApiConfig,
    request: Request,
) -> JSONResponse:
    existing = await get_gateway_api(api_id)
    if existing is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "gateway_api_not_found",
                "message": "API locale non trovata.",
            },
            headers={"Cache-Control": "no-store"},
        )
    await token_budget.expire_reservations()
    routing_changed = (
        existing.provider != payload.provider
        or existing.model != payload.model
        or existing.reasoning_effort != payload.reasoning_effort
    )
    if routing_changed:
        invalid = await _validate_gateway_api_config(request.app, payload)
        if invalid is not None:
            return invalid
    try:
        record = await update_gateway_api(
            api_id,
            name=payload.name,
            provider=payload.provider,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
            skill_ids=payload.skill_ids,
            mcp_tools=payload.flattened_mcp_tools,
            limits=payload.limits.model_dump() if payload.limits is not None else None,
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=409,
            content={"error": "gateway_api_conflict", "message": str(exc)},
            headers={"Cache-Control": "no-store"},
        )
    if record is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "gateway_api_not_found",
                "message": "API locale non trovata.",
            },
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        content=record.as_dict(),
        headers={"Cache-Control": "no-store"},
    )


@app.delete(
    "/api/gateway-apis/{api_id}",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def gateway_api_delete(api_id: int) -> JSONResponse:
    deleted = await delete_gateway_api(api_id)
    if not deleted:
        return JSONResponse(
            status_code=404,
            content={
                "error": "gateway_api_not_found",
                "message": "API locale non trovata.",
            },
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        content={"status": "deleted"},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/build/projects", include_in_schema=False)
async def build_project_list() -> JSONResponse:
    return JSONResponse(
        content={"data": await list_build_projects()},
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/build/projects",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def build_project_create(
    payload: BuildProjectConfig,
    request: Request,
) -> JSONResponse:
    invalid = await _validate_build_models(
        request.app,
        payload.analyst,
        payload.builder,
    )
    if invalid is not None:
        return invalid
    try:
        files = _sanitize_build_files(payload.files)
    except ValueError as exc:
        return _build_error(400, "invalid_project_snapshot", str(exc))
    try:
        project = await create_build_project(
            name=payload.name,
            folder_name=payload.folder_name,
            idea=payload.idea,
            analyst_mode=payload.analyst_mode,
            analyst_provider=payload.analyst.provider,
            analyst_model=payload.analyst.model,
            analyst_reasoning_effort=payload.analyst.reasoning_effort,
            builder_provider=payload.builder.provider,
            builder_model=payload.builder.model,
            builder_reasoning_effort=payload.builder.reasoning_effort,
            files=files,
        )
    except ValueError as exc:
        return _build_error(409, "build_project_conflict", str(exc))
    return JSONResponse(
        status_code=201,
        content=project,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/build/projects/{project_id}", include_in_schema=False)
async def build_project_detail(project_id: int) -> JSONResponse:
    project = await get_build_project(project_id)
    if project is None:
        return _build_error(
            404,
            "build_project_not_found",
            "Progetto Build non trovato.",
        )
    files, artifacts, messages, phases = await asyncio.gather(
        list_build_project_files(project_id),
        list_build_artifacts(project_id),
        list_build_messages(project_id),
        list_build_phases(project_id),
    )
    return JSONResponse(
        content={
            **project,
            "files": files,
            "artifacts": artifacts,
            "messages": messages,
            "phases": phases,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get(
    "/api/build/projects/{project_id}/activity",
    include_in_schema=False,
)
async def build_project_activity(
    project_id: int,
    request: Request,
) -> JSONResponse:
    activity = request.app.state.build_activities.get(project_id)
    return JSONResponse(
        content=activity or {"active": False},
        headers={"Cache-Control": "no-store"},
    )


@app.patch(
    "/api/build/projects/{project_id}",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def build_project_update(
    project_id: int,
    payload: BuildProjectConfig,
    request: Request,
) -> JSONResponse:
    existing = await get_build_project(project_id)
    if existing is None:
        return _build_error(
            404,
            "build_project_not_found",
            "Progetto Build non trovato.",
        )
    invalid = await _validate_build_models(
        request.app,
        payload.analyst,
        payload.builder,
    )
    if invalid is not None:
        return invalid
    files: list[dict[str, str]] | None = None
    if payload.files:
        try:
            files = _sanitize_build_files(payload.files)
        except ValueError as exc:
            return _build_error(400, "invalid_project_snapshot", str(exc))
    try:
        project = await update_build_project(
            project_id,
            name=payload.name,
            folder_name=payload.folder_name,
            idea=payload.idea,
            analyst_mode=payload.analyst_mode,
            analyst_provider=payload.analyst.provider,
            analyst_model=payload.analyst.model,
            analyst_reasoning_effort=payload.analyst.reasoning_effort,
            builder_provider=payload.builder.provider,
            builder_model=payload.builder.model,
            builder_reasoning_effort=payload.builder.reasoning_effort,
        )
        if files is not None:
            project = await replace_build_project_files(
                project_id,
                folder_name=payload.folder_name,
                files=files,
            )
    except ValueError as exc:
        return _build_error(409, "build_project_conflict", str(exc))
    if project is None:
        return _build_error(
            404,
            "build_project_not_found",
            "Progetto Build non trovato.",
        )
    return JSONResponse(
        content=project,
        headers={"Cache-Control": "no-store"},
    )


@app.put(
    "/api/build/projects/{project_id}/files",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def build_project_file_sync(
    project_id: int,
    payload: BuildFileSync,
) -> JSONResponse:
    try:
        files = _sanitize_build_files(payload.files)
    except ValueError as exc:
        return _build_error(400, "invalid_project_snapshot", str(exc))
    project = await replace_build_project_files(
        project_id,
        folder_name=payload.folder_name,
        files=files,
    )
    if project is None:
        return _build_error(
            404,
            "build_project_not_found",
            "Progetto Build non trovato.",
        )
    return JSONResponse(
        content=project,
        headers={"Cache-Control": "no-store"},
    )


@app.delete(
    "/api/build/projects/{project_id}",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def build_project_delete(project_id: int) -> JSONResponse:
    if not await delete_build_project(project_id):
        return _build_error(
            404,
            "build_project_not_found",
            "Progetto Build non trovato.",
        )
    return JSONResponse(
        content={"status": "deleted"},
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/build/projects/{project_id}/plan",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def build_project_plan(
    project_id: int,
    payload: BuildPlanRequest,
    request: Request,
) -> JSONResponse:
    project = await get_build_project(project_id)
    if project is None:
        return _build_error(
            404,
            "build_project_not_found",
            "Progetto Build non trovato.",
        )
    analyst = BuildModelConfig.model_validate(project["analyst"])
    try:
        await _build_model_route(
            request.app,
            analyst,
            role_label="Analista idea",
        )
        project = await update_build_project(
            project_id,
            name=str(project["name"]),
            folder_name=str(project["folder_name"]),
            idea=payload.idea,
            analyst_mode=str(project["analyst_mode"]),
            analyst_provider=analyst.provider,
            analyst_model=analyst.model,
            analyst_reasoning_effort=analyst.reasoning_effort,
            builder_provider=str(project["builder"]["provider"]),
            builder_model=str(project["builder"]["model"]),
            builder_reasoning_effort=str(
                project["builder"]["reasoning_effort"]
            ),
        )
        context = await build_project_context(project_id)
        artifacts = await _run_build_planning_pipeline(
            request,
            analyst=analyst,
            analyst_mode=str(project["analyst_mode"]),
            idea=payload.idea,
            project_context=context,
        )
        prepared_artifacts: list[tuple[str, str]] = []
        planned_phases: list[dict[str, str]] = []
        for artifact_type, content in artifacts:
            if artifact_type == "roadmap":
                clean_content, planned_phases = _extract_analyst_plan(content)
                prepared_artifacts.append((artifact_type, clean_content))
            else:
                prepared_artifacts.append((artifact_type, content))
        saved = await replace_build_artifacts(
            project_id,
            prepared_artifacts,
        )
        phases = (
            await replace_build_phases(project_id, planned_phases)
            if planned_phases
            else await list_build_phases(project_id)
        )
    except (GatewayError, ProviderBrokerError) as exc:
        message = getattr(exc, "message", str(exc))
        status_code = getattr(exc, "status_code", 502)
        return _build_error(
            status_code,
            str(getattr(exc, "code", "build_pipeline_failed")),
            message or "Il modello non ha completato la pipeline.",
        )
    if project is None:
        return _build_error(
            404,
            "build_project_not_found",
            "Progetto Build non trovato.",
        )
    return JSONResponse(
        content={
            "project": project,
            "artifacts": saved,
            "phases": phases,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/build/projects/{project_id}/chat",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def build_project_chat(
    project_id: int,
    payload: BuildChatRequest,
    request: Request,
) -> JSONResponse:
    project = await get_build_project(project_id)
    if project is None:
        return _build_error(
            404,
            "build_project_not_found",
            "Progetto Build non trovato.",
        )
    model_config = BuildModelConfig.model_validate(project[payload.lane])
    await add_build_message(
        project_id,
        lane=payload.lane,
        role="user",
        content=payload.message,
    )
    messages, artifacts, project_files = await asyncio.gather(
        list_build_messages(project_id, limit=30),
        list_build_artifacts(project_id),
        build_project_context(project_id, max_characters=32_000),
    )
    history = [
        item
        for item in messages
        if item["lane"] == payload.lane
    ][-12:]
    if payload.lane == "builder":
        # I percorsi del comando corrente hanno priorità assoluta. Solo per un
        # follow-up generico come "procedi" si risale un messaggio alla volta:
        # unendo tutto lo storico si selezionavano file estranei citati in
        # vecchi brief, consumando il budget prima dei file realmente richiesti.
        fallback_references = [
            _trim_build_text(str(item["content"]), 12_000)
            for item in reversed(history[:-1])
            if not (
                item.get("role") == "assistant"
                and _legacy_readonly_builder_answer(
                    str(item.get("content", ""))
                )
            )
        ]
        fallback_references.extend(
            _trim_build_text(str(item["content"]), 8_000)
            for item in artifacts
        )
        project_files = await _targeted_builder_context(
            project_id,
            payload.message,
            fallback_texts=fallback_references,
        )
    _set_build_activity(
        request.app,
        project_id,
        role=payload.lane,
        phase="chat",
    )
    try:
        if payload.lane == "builder":
            answer = await _run_builder_with_context_recovery(
                request,
                project=project,
                model_config=model_config,
                history=history,
                artifacts=artifacts,
                project_context=project_files,
                request_text=payload.message,
            )
        else:
            answer = await _run_build_chat(
                request,
                project=project,
                model_config=model_config,
                lane=payload.lane,
                history=history,
                artifacts=artifacts,
                project_context=project_files,
            )
    except (GatewayError, ProviderBrokerError) as exc:
        message = getattr(exc, "message", str(exc))
        status_code = getattr(exc, "status_code", 502)
        return _build_error(
            status_code,
            str(getattr(exc, "code", "build_chat_failed")),
            message or "Il modello non ha risposto.",
        )
    finally:
        _clear_build_activity(request.app, project_id)
    planned_phases: list[dict[str, str]] = []
    saved_answer = answer
    if payload.lane == "analyst":
        saved_answer, planned_phases = _extract_analyst_plan(answer)
    saved = await add_build_message(
        project_id,
        lane=payload.lane,
        role="assistant",
        content=saved_answer,
    )
    phases = await list_build_phases(project_id)
    if planned_phases:
        phases = await replace_build_phases(
            project_id,
            planned_phases,
            source_message_id=int(saved["id"]),
        )
    changes: list[dict[str, object]] = []
    change_error: str | None = None
    if payload.lane == "builder":
        changes, change_error = await _materialize_builder_changes(
            project_id,
            answer,
        )
    handoff: dict[str, object] | None = None
    if (
        payload.lane == "analyst"
        and _requests_builder_handoff(payload.message)
    ):
        try:
            handoff = await _run_builder_handoff(
                request,
                project=project,
                source_message=saved,
                instruction=payload.message,
                artifacts=artifacts,
                project_context=project_files,
            )
        except (GatewayError, ProviderBrokerError) as exc:
            handoff = {
                "status": "failed",
                "error": str(
                    getattr(exc, "code", "build_handoff_failed")
                ),
                "message": (
                    getattr(exc, "message", str(exc))
                    or "Il Builder non ha ricevuto la consegna."
                ),
            }
        phases = await list_build_phases(project_id)
    _clear_build_activity(request.app, project_id)
    return JSONResponse(
        content={
            **saved,
            "handoff": handoff,
            "changes": changes,
            "change_error": change_error,
            "phases": phases,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/build/projects/{project_id}/resume",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def build_project_resume(
    project_id: int,
    request: Request,
) -> JSONResponse:
    """Rivaluta e rilancia la prima fase bloccata senza azzerare la catena."""
    project = await get_build_project(project_id)
    if project is None:
        return _build_error(
            404,
            "build_project_not_found",
            "Progetto Build non trovato.",
        )
    phases = await list_build_phases(project_id)
    blocked_phase = next(
        (phase for phase in phases if phase["status"] == "blocked"),
        None,
    )
    if blocked_phase is None:
        return _build_error(
            409,
            "build_phase_not_blocked",
            "Non esiste una fase bloccata da riprendere.",
        )

    completed_positions = [
        str(phase["position"])
        for phase in phases
        if phase["status"] == "completed"
    ]
    resume_request = (
        "Rivaluta esclusivamente la fase bloccata "
        f"{blocked_phase['position']} “{blocked_phase['title']}”. "
        f"Errore precedente: {blocked_phase.get('error') or 'patch non valida'}. "
        "Fornisci una correzione operativa breve per il Builder. Le fasi già "
        f"completate ({', '.join(completed_positions) or 'nessuna'}) sono "
        "immutabili: non vanno ripetute e non va rigenerata la checklist. "
        "Al termine passa la correzione al Builder."
    )
    await add_build_message(
        project_id,
        lane="analyst",
        role="user",
        content=resume_request,
    )
    messages, artifacts, project_context = await asyncio.gather(
        list_build_messages(project_id, limit=30),
        list_build_artifacts(project_id),
        build_project_context(project_id, max_characters=32_000),
    )
    history = [
        message for message in messages if message["lane"] == "analyst"
    ][-12:]
    analyst = BuildModelConfig.model_validate(project["analyst"])
    _set_build_activity(
        request.app,
        project_id,
        role="analyst",
        phase="resume",
    )
    try:
        answer = await _run_build_chat(
            request,
            project=project,
            model_config=analyst,
            lane="analyst",
            history=history,
            artifacts=artifacts,
            project_context=project_context,
        )
    except (GatewayError, ProviderBrokerError) as exc:
        return _build_error(
            getattr(exc, "status_code", 502),
            str(getattr(exc, "code", "build_resume_failed")),
            (
                getattr(exc, "message", str(exc))
                or "L’Analista non ha completato la rivalutazione."
            ),
        )
    finally:
        _clear_build_activity(request.app, project_id)

    # Durante un resume un eventuale nuovo blocco <omniproxy-plan> viene
    # volutamente ignorato: la checklist e le fasi completate restano intatte.
    clean_answer, _ignored_plan = _extract_analyst_plan(answer)
    if not clean_answer.strip():
        clean_answer = (
            "Riprova la fase corrente usando lo snapshot aggiornato e "
            "correggendo l’errore riportato dalla precedente esecuzione."
        )
    analyst_message = await add_build_message(
        project_id,
        lane="analyst",
        role="assistant",
        content=clean_answer,
    )
    try:
        handoff = await _advance_builder_chain(
            request,
            project=project,
            instruction=clean_answer,
        )
    except (GatewayError, ProviderBrokerError) as exc:
        handoff = {
            "status": "blocked",
            "message": (
                getattr(exc, "message", str(exc))
                or "La fase bloccata non è ripartita."
            ),
            "changes": [],
            "change_error": str(
                getattr(exc, "code", "build_resume_failed")
            ),
            "phases": await list_build_phases(project_id),
        }
    finally:
        _clear_build_activity(request.app, project_id)
    return JSONResponse(
        content={
            "status": "resumed",
            "analyst_message": analyst_message,
            "handoff": handoff,
            "phases": await list_build_phases(project_id),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/build/projects/{project_id}/handoff",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def build_project_handoff(
    project_id: int,
    payload: BuildHandoffRequest,
    request: Request,
) -> JSONResponse:
    project = await get_build_project(project_id)
    if project is None:
        return _build_error(
            404,
            "build_project_not_found",
            "Progetto Build non trovato.",
        )
    messages, artifacts, project_files = await asyncio.gather(
        list_build_messages(project_id, limit=80),
        list_build_artifacts(project_id),
        build_project_context(project_id, max_characters=32_000),
    )
    source_message = next(
        (
            message
            for message in reversed(messages)
            if message["lane"] == "analyst"
            and message["role"] == "assistant"
        ),
        None,
    )
    if source_message is None:
        builder_brief = next(
            (
                artifact
                for artifact in artifacts
                if artifact["artifact_type"] == "builder_brief"
            ),
            None,
        )
        if builder_brief is not None:
            source_message = {
                "id": None,
                "content": builder_brief["content"],
            }
    if source_message is None:
        return _build_error(
            409,
            "build_handoff_source_missing",
            (
                "Prima chiedi una risposta all’Analista oppure genera "
                "il brief della pipeline."
            ),
        )
    try:
        result = await _run_builder_handoff(
            request,
            project=project,
            source_message=source_message,
            instruction=payload.instruction,
            artifacts=artifacts,
            project_context=project_files,
        )
    except (GatewayError, ProviderBrokerError) as exc:
        return _build_error(
            getattr(exc, "status_code", 502),
            str(getattr(exc, "code", "build_handoff_failed")),
            (
                getattr(exc, "message", str(exc))
                or "Il Builder non ha ricevuto la consegna."
            ),
        )
    finally:
        _clear_build_activity(request.app, project_id)
    return JSONResponse(
        status_code=201 if result["status"] == "completed" else 200,
        content=result,
        headers={"Cache-Control": "no-store"},
    )


@app.get(
    "/api/build/projects/{project_id}/builder-proposal",
    include_in_schema=False,
)
async def build_project_builder_proposal(
    project_id: int,
) -> JSONResponse:
    project = await get_build_project(project_id)
    if project is None:
        return _build_error(
            404,
            "build_project_not_found",
            "Progetto Build non trovato.",
        )
    messages = await list_build_messages(project_id, limit=100)
    builder_message = next(
        (
            message
            for message in reversed(messages)
            if message["lane"] == "builder"
            and message["role"] == "assistant"
        ),
        None,
    )
    if (
        builder_message is None
        or not _BUILDER_CHANGES_PATTERN.search(
            str(builder_message["content"])
        )
        or builder_message.get("changes_applied_at") is not None
    ):
        return JSONResponse(
            content={
                "message_id": None,
                "changes": [],
                "change_error": None,
            },
            headers={"Cache-Control": "no-store"},
        )
    changes, change_error = await _materialize_builder_changes(
        project_id,
        str(builder_message["content"]),
    )
    return JSONResponse(
        content={
            "message_id": builder_message["id"],
            "changes": changes,
            "change_error": change_error,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/build/projects/{project_id}/builder-proposals/"
    "{message_id}/applied",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def build_project_builder_proposal_applied(
    project_id: int,
    message_id: int,
    request: Request,
) -> JSONResponse:
    marked = await mark_build_message_changes_applied(
        project_id,
        message_id,
    )
    if not marked:
        return _build_error(
            404,
            "builder_proposal_not_found",
            "Proposta Builder non trovata.",
        )
    completed_phase = await complete_build_phase_for_message(
        project_id,
        message_id,
    )
    next_phase: dict[str, object] | None = None
    command_job: dict[str, object] | None = None
    if completed_phase is not None:
        project = await get_build_project(project_id)
        if project is not None:
            try:
                next_phase = await _advance_builder_chain(
                    request,
                    project=project,
                )
            except (GatewayError, ProviderBrokerError) as exc:
                next_phase = {
                    "status": "blocked",
                    "message": (
                        getattr(exc, "message", str(exc))
                        or "La fase successiva non è partita."
                    ),
                    "changes": [],
                    "change_error": str(
                        getattr(exc, "code", "build_chain_failed")
                    ),
                    "phases": await list_build_phases(project_id),
                }
            finally:
                _clear_build_activity(request.app, project_id)
        current_phases = await list_build_phases(project_id)
        if project is not None and current_phases and all(
            phase["status"] == "completed" for phase in current_phases
        ):
            messages = await list_build_messages(project_id, limit=100)
            phase_message_ids = {
                int(phase["builder_message_id"])
                for phase in current_phases
                if isinstance(phase.get("builder_message_id"), int)
            }
            commands, command_error = _collect_maintenance_commands(
                [
                    str(message["content"])
                    for message in messages
                    if int(message["id"]) in phase_message_ids
                ]
            )
            if command_error is not None:
                command_job = {
                    "status": "rejected",
                    "message": command_error,
                }
            elif commands:
                command_job = await _submit_maintenance_job(
                    request,
                    project,
                    commands,
                )
    return JSONResponse(
        content={
            "status": "applied",
            "message_id": message_id,
            "completed_phase": completed_phase,
            "next_phase": next_phase,
            "command_job": command_job,
            "phases": await list_build_phases(project_id),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/build/projects/{project_id}/maintenance/rebuild",
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_mutation)],
)
async def build_project_maintenance_rebuild(
    project_id: int,
    request: Request,
) -> JSONResponse:
    """Queue the single fixed Docker rebuild action for OmniProxy itself."""
    project = await get_build_project(project_id)
    if project is None:
        return _build_error(
            404,
            "build_project_not_found",
            "Progetto Build non trovato.",
        )
    job = await _submit_maintenance_job(
        request,
        project,
        ["docker compose up -d --build --force-recreate"],
    )
    if job is None:
        return _build_error(
            503,
            "maintenance_runner_unavailable",
            "Il runner Docker non ha accettato l’aggiornamento.",
        )
    status = str(job.get("status", "unavailable"))
    return JSONResponse(
        status_code=202 if status == "queued" else 409,
        content=job,
        headers={"Cache-Control": "no-store"},
    )


@app.get(
    "/api/build/projects/{project_id}/maintenance/jobs/{job_id}",
    include_in_schema=False,
)
async def build_project_maintenance_job(
    project_id: int,
    job_id: str,
    request: Request,
) -> JSONResponse:
    project = await get_build_project(project_id)
    if project is None:
        return _build_error(
            404,
            "build_project_not_found",
            "Progetto Build non trovato.",
        )
    if str(project.get("folder_name", "")) != (
        settings.maintenance_workspace_name
    ):
        return _build_error(
            403,
            "maintenance_workspace_not_allowed",
            "Questo progetto non può gestire i container OmniProxy.",
        )
    try:
        normalized_job_id = str(uuid.UUID(job_id))
    except ValueError:
        return _build_error(
            404,
            "maintenance_job_not_found",
            "Operazione Docker non trovata.",
        )
    try:
        response = await request.app.state.maintenance_http.get(
            f"/v1/jobs/{normalized_job_id}",
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return _build_error(
            503,
            "maintenance_runner_unavailable",
            "Il runner Docker non è momentaneamente raggiungibile.",
        )
    if response.status_code == 404:
        return _build_error(
            404,
            "maintenance_job_not_found",
            "Operazione Docker non trovata.",
        )
    if not response.is_success or not isinstance(payload, dict):
        return _build_error(
            502,
            "maintenance_job_failed",
            "Il runner Docker ha restituito uno stato non valido.",
        )
    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/v1/models")
async def models(request: Request) -> dict[str, object]:
    binding = await get_gateway_api_for_key(int(request.state.api_key_id))
    if binding is not None:
        return {
            "object": "list",
            "data": [
                {
                    "id": binding.slug,
                    "object": "model",
                    "created": 0,
                    "owned_by": f"omni-proxy-{binding.provider}",
                }
            ],
        }
    return {"object": "list", "data": public_models()}


@app.get("/v1/usage")
async def key_usage(request: Request):
    owner = int(request.state.api_key_id)
    return {"limits": (await token_budget.policy(owner)).model_dump(), "usage": await token_budget.status(owner), "technical_limits": {"ollama_context_tokens": settings.ollama_context_tokens}}


@app.post("/v1/chat/completions")
async def chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
):
    started = time.perf_counter()
    request_id = f"chatcmpl-{uuid.uuid4().hex}"
    api_key_id = int(request.state.api_key_id)
    request.state.deployment_trace = deployments.start_trace()
    binding = await get_gateway_api_for_key(api_key_id)
    payload = await await_connected(request, resolve_chat_files(request, payload))
    capability_context = (
        await load_capability_context(binding.id)
        if binding is not None
        else CapabilityContext((), ())
    )
    effective_payload = apply_capability_context(payload, capability_context)
    injected_capability = effective_payload.messages[0] if effective_payload is not payload else None

    # CONTEGGIO TOKEN IN INGRESSO:
    # Include anche le Skill amministrative e il catalogo dei tool MCP
    # effettivamente inviati al provider. Le iterazioni MCP successive vengono
    # sommate da `complete_with_mcp`.
    prompt_tokens = count_request_tokens(effective_payload)

    # ROUTING DINAMICO:
    # `model` è un alias pubblico. La registry lo traduce in provider e modello
    # effettivo senza permettere al client di passare URL o credenziali.
    try:
        route = (
            ModelRoute(
                requested_model=payload.model,
                provider=binding.provider,
                resolved_model=binding.model,
                reasoning_effort=binding.reasoning_effort,
            )
            if binding is not None
            else resolve_model_route(payload.model)
        )
    except GatewayError as exc:
        await _record_usage_safely(
            request_id=request_id,
            api_key_id=api_key_id,
            route=ModelRoute(payload.model, "unresolved", "unresolved"),
            prompt_tokens=0,
            completion_tokens=0,
            status_code=exc.status_code,
            started=started,
            error_code=exc.code,
        )
        raise

    if payload.has_client_tools and route.provider not in {"codex", "openai_compatible"}:
        raise GatewayError(400, "Gli strumenti del client non sono disponibili per questo provider.", code="client_tools_not_supported")
    if payload.has_client_tools and capability_context.has_mcp_tools:
        raise GatewayError(400, "Gli strumenti del client e quelli MCP gestiti non possono essere eseguiti nella stessa richiesta.", code="tool_mode_conflict")
    if payload.has_client_tools and payload.has_images and route.provider == "codex":
        raise GatewayError(400, "Il percorso strumenti Codex non supporta ancora immagini nella stessa richiesta.", code="client_tools_images_not_supported")
    session = InferenceLimits(api_key_id, await token_budget.policy(api_key_id), request_id, lambda current: _complete_without_tools(request, route, current), physical_context_tokens=settings.ollama_context_tokens if route.provider == "ollama" else None, telemetry=request.app.state.telemetry, provider_name=route.provider, resolved_model=route.resolved_model)
    request.state.limit_session = session
    try:
        effective_payload = await await_connected(request, session.prepare(effective_payload))
        if payload.stream and not payload.has_client_tools:
            effective_payload = await session.pre_reserve(effective_payload)
        request.state.effective_messages = [message for message in effective_payload.messages if message is not injected_capability]
    except GatewayError as exc:
        await _record_usage_safely(request_id=request_id, api_key_id=api_key_id, route=route, prompt_tokens=session.prompt_tokens, completion_tokens=session.completion_tokens, status_code=exc.status_code, started=started, error_code=exc.code)
        raise

    if payload.has_client_tools:
        if route.provider not in {"codex", "openai_compatible"}:
            raise GatewayError(400, "Gli strumenti del client non sono disponibili per questo provider.", code="client_tools_not_supported")
        if capability_context.has_mcp_tools:
            raise GatewayError(400, "Gli strumenti del client e quelli MCP gestiti non possono essere eseguiti nella stessa richiesta.", code="tool_mode_conflict")
        try:
            if route.provider == "codex":
                async def operation(current):
                    return await _broker_client_for_provider(request, "codex").complete_with_tools(current, model=route.resolved_model, reasoning_effort=route.reasoning_effort)
            else:
                async def operation(current):
                    return await OpenAICompatibleProvider().complete_with_tools(current, route.resolved_model)
            tool_result = await await_connected(request, session.tools(effective_payload, operation))
        except (ProviderBrokerError, GatewayError) as exc:
            await _record_usage_safely(
                request_id=request_id, api_key_id=api_key_id, route=route,
                prompt_tokens=session.prompt_tokens, completion_tokens=session.completion_tokens,
                status_code=exc.status_code, started=started, error_code=exc.code,
            )
            if isinstance(exc, GatewayError):
                raise
            raise GatewayError(exc.status_code, str(exc), error_type="provider_error", code=exc.code) from exc
        content = tool_result.get("content")
        calls = tool_result.get("tool_calls") or []
        finish_reason = "tool_calls" if calls else ("length" if session.output_truncated else "stop")
        prompt_tokens, completion_tokens = session.prompt_tokens, session.completion_tokens
        await _record_usage_safely(
            request_id=request_id, api_key_id=api_key_id, route=route,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            status_code=200, started=started,
        )
        response_model = binding.slug if binding is not None else payload.model
        if payload.stream:
            async def tool_events():
                created = int(time.time())
                yield _sse(_stream_chunk(request_id, created, response_model, {"role": "assistant", "content": ""}))
                if content:
                    yield _sse(_stream_chunk(request_id, created, response_model, {"content": content}))
                for index, call in enumerate(calls):
                    yield _sse(_stream_chunk(request_id, created, response_model, {"tool_calls": [{"index": index, **call}]}))
                yield _sse(_stream_chunk(request_id, created, response_model, {}, finish_reason=finish_reason))
                if payload.stream_options and payload.stream_options.include_usage:
                    yield _sse({"id": request_id, "object": "chat.completion.chunk", "created": created, "model": response_model, "choices": [], "usage": _usage(prompt_tokens, completion_tokens)})
                yield "data: [DONE]\n\n"
            return StreamingResponse(tool_events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": request_id, **await session.headers()})
        message = {"role": "assistant", "content": content, "refusal": None}
        if calls:
            message["tool_calls"] = calls
        return JSONResponse(
            headers={"X-Request-ID": request_id, **await session.headers()},
            content={"id": request_id, "object": "chat.completion", "created": int(time.time()), "model": response_model, "choices": [{"index": 0, "message": message, "logprobs": None, "finish_reason": finish_reason}], "usage": _usage(prompt_tokens, completion_tokens)},
        )

    if payload.stream:
        return StreamingResponse(
            _stream_completion(
                payload=effective_payload,
                request=request,
                route=route,
                request_id=request_id,
                api_key_id=api_key_id,
                prompt_tokens=prompt_tokens,
                started=started,
                response_model=binding.slug if binding is not None else payload.model,
                capability_context=capability_context,
                gateway_api_id=binding.id if binding is not None else None,
            ),
            media_type="text/event-stream",
            background=BackgroundTask(session.release_unstarted),
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Request-ID": request_id,
                **await session.headers(),
            },
        )

    response_model = binding.slug if binding is not None else payload.model
    try:
        if binding is not None:
            capability_result = await await_connected(request, complete_with_mcp(
                payload=effective_payload,
                context=capability_context,
                complete=lambda current: _provider_complete(
                    current,
                    request,
                    route,
                ),
                request_id=request_id,
                gateway_api_id=binding.id,
            ))
            content = capability_result.content
            prompt_tokens = capability_result.prompt_tokens
        else:
            content = await await_connected(request, _provider_complete(
                effective_payload,
                request,
                route,
            ))
    except GatewayError as exc:
        await _record_usage_safely(
            request_id=request_id,
            api_key_id=api_key_id,
            route=route,
            prompt_tokens=session.prompt_tokens,
            completion_tokens=session.completion_tokens,
            status_code=exc.status_code,
            started=started,
            error_code=exc.code,
        )
        raise

    # CONTEGGIO TOKEN IN USCITA E PERSISTENZA:
    # il log viene associato all'id della API key autenticata dal middleware.
    prompt_tokens = session.prompt_tokens or prompt_tokens
    completion_tokens = session.completion_tokens
    await _record_usage_safely(
        request_id=request_id,
        api_key_id=api_key_id,
        route=route,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        status_code=200,
        started=started,
    )

    return JSONResponse(
        headers={"X-Request-ID": request_id, **await session.headers()},
        content={
            "id": request_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": response_model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "refusal": None,
                    },
                    "logprobs": None,
                    "finish_reason": "length" if session.output_truncated else "stop",
                }
            ],
            "usage": _usage(prompt_tokens, completion_tokens),
        },
    )


def _limit_headers(response: Response) -> dict[str, str]:
    return {key: value for key, value in response.headers.items() if key.lower().startswith("x-omniproxy-")}


@app.post("/v1/responses")
async def create_response(payload: ResponsesRequest, request: Request):
    api_key_id = int(request.state.api_key_id)
    history: list[dict[str, object]] = []
    if payload.previous_response_id is not None:
        stored = await get_stored_response(payload.previous_response_id, api_key_id)
        if stored is None:
            raise GatewayError(404, "Risposta precedente non trovata.", code="response_not_found")
        prior_model, prior_input, prior_response = stored
        if prior_model != payload.model:
            raise GatewayError(400, "Il modello della conversazione non coincide.", code="response_model_mismatch")
        history = [*prior_input, *prior_response["output"]]
    current_input = input_items(payload)
    full_input = [*history, *current_input]
    if len(full_input) > 256 or len(json.dumps(full_input)) > 2_000_000:
        raise GatewayError(400, "Cronologia Responses troppo grande.", code="response_history_too_large")
    effective_payload = payload.model_copy(
        update={"input": full_input, "instructions": None, "previous_response_id": None}
    )
    chat_response = await chat_completions(to_chat_request(effective_payload), request)
    stored_input = from_chat_input(request.state.effective_messages)
    response_id = f"resp_{uuid.uuid4().hex}"
    if payload.stream:
        async def response_events():
            async for event in stream_chat_response(
                chat_response, response_id, payload.model, store=payload.store,
                previous_response_id=payload.previous_response_id,
            ):
                if payload.store and event.startswith(("event: response.completed\n", "event: response.incomplete\n")):
                    completed = json.loads(event.split("data: ", 1)[1])["response"]
                    await save_stored_response(
                        response_id, api_key_id, payload.model, stored_input, completed
                    )
                yield event

        return StreamingResponse(
            response_events(),
            media_type="text/event-stream",
            background=chat_response.background,
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Request-ID": response_id,
                **_limit_headers(chat_response),
            },
        )
    chat_body = json.loads(chat_response.body)
    response_body = from_chat_response(chat_body, response_id)
    response_body["store"] = payload.store
    response_body["previous_response_id"] = payload.previous_response_id
    if payload.store:
        await save_stored_response(
            response_id, api_key_id, payload.model, stored_input, response_body
        )
    return JSONResponse(
        content=response_body,
        headers={"X-Request-ID": response_id, **_limit_headers(chat_response)},
    )


@app.get("/v1/responses/{response_id}")
async def retrieve_response(response_id: str, request: Request):
    stored = await get_stored_response(response_id, int(request.state.api_key_id))
    if stored is None:
        raise GatewayError(404, "Risposta non trovata.", code="response_not_found")
    return stored[2]


@app.delete("/v1/responses/{response_id}")
async def delete_response(response_id: str, request: Request):
    deleted = await delete_stored_response(response_id, int(request.state.api_key_id))
    if not deleted:
        raise GatewayError(404, "Risposta non trovata.", code="response_not_found")
    return {"id": response_id, "object": "response.deleted", "deleted": True}


@app.post("/v1/messages")
async def create_native_message(payload: AnthropicRequest, request: Request):
    chat_response = await chat_completions(anthropic_to_chat_request(payload), request)
    message_id = f"msg_{uuid.uuid4().hex}"
    if payload.stream:
        return StreamingResponse(
            anthropic_stream_chat_response(chat_response, message_id, payload.model),
            media_type="text/event-stream",
            background=chat_response.background,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": message_id, **_limit_headers(chat_response)},
        )
    return JSONResponse(
        content=anthropic_from_chat_response(json.loads(chat_response.body), message_id),
        headers={"X-Request-ID": message_id, **_limit_headers(chat_response)},
    )


@app.post("/v1beta/models/{model}:generateContent")
@app.post("/v1/models/{model}:generateContent")
async def create_gemini_content(model: str, payload: GeminiRequest, request: Request):
    chat_response = await chat_completions(gemini_to_chat_request(payload, model, stream=False), request)
    response_id = f"gem_{uuid.uuid4().hex}"
    return JSONResponse(
        content=gemini_from_chat_response(json.loads(chat_response.body), response_id),
        headers={"X-Request-ID": response_id, **_limit_headers(chat_response)},
    )


@app.post("/v1beta/models/{model}:streamGenerateContent")
@app.post("/v1/models/{model}:streamGenerateContent")
async def stream_gemini_content(model: str, payload: GeminiRequest, request: Request):
    chat_response = await chat_completions(gemini_to_chat_request(payload, model, stream=True), request)
    response_id = f"gem_{uuid.uuid4().hex}"
    return StreamingResponse(
        gemini_stream_chat_response(chat_response, response_id, model),
        media_type="text/event-stream",
        background=chat_response.background,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": response_id, **_limit_headers(chat_response)},
    )


async def _provider_complete(
    payload: ChatCompletionRequest,
    request: Request,
    route: ModelRoute,
) -> str:
    provider = await _provider_for_route(request, route)
    session = getattr(request.state, "limit_session", None)
    if session is not None:
        return await session.complete(payload, lambda current: provider.complete(current, route.resolved_model))
    return await provider.complete(payload, route.resolved_model)


async def _provider_for_route(request: Request, route: ModelRoute) -> TextProvider:
    if route.provider == "ollama":
        client = await _require_ollama_client(request.app)
        return OllamaProvider(
            client,
            think=_ollama_think_for_route(route),
        )
    if route.provider in {"codex", "gemini", "claude"}:
        return CliBrokerProvider(
            _broker_client_for_provider(request, route.provider),
            route,
        )
    if route.provider == "openai_compatible":
        return OpenAICompatibleProvider()
    return ExternalReasoningMockProvider()


async def _job_complete(app_instance: FastAPI, profile, payload: ChatCompletionRequest) -> str:
    request = Request({"type": "http", "app": app_instance, "headers": [], "state": {}})
    route = ModelRoute(profile.slug, profile.provider, profile.model, profile.reasoning_effort)
    request_id = f"jobcall-{uuid.uuid4().hex}"
    started = time.perf_counter()
    session = InferenceLimits(profile.api_key_id, await token_budget.policy(profile.api_key_id), request_id, lambda current: _complete_without_tools(request, route, current), physical_context_tokens=settings.ollama_context_tokens if route.provider == "ollama" else None, telemetry=app_instance.state.telemetry, provider_name=route.provider, resolved_model=route.resolved_model)
    telemetry_token = observability.CURRENT.set(app_instance.state.telemetry)
    status, code = 200, None
    try:
        return await session.complete(payload, lambda current: _complete_without_tools(request, route, current))
    except asyncio.CancelledError:
        status, code = 499, "job_cancelled"
        raise
    except (GatewayError, ProviderBrokerError) as exc:
        status, code = exc.status_code, exc.code
        raise
    finally:
        try:
            await asyncio.shield(_record_usage_safely(request_id=request_id, api_key_id=profile.api_key_id, route=route, prompt_tokens=session.prompt_tokens, completion_tokens=session.completion_tokens, status_code=status, started=started, error_code=code))
        finally:
            observability.CURRENT.reset(telemetry_token)


async def _complete_without_tools(request: Request, route: ModelRoute, payload: ChatCompletionRequest) -> str:
    if route.provider in {"codex", "openai_compatible"}:
        no_tools = payload.model_copy(update={"tools": [], "tool_choice": "none", "parallel_tool_calls": False})
        if route.provider == "codex":
            result = await _broker_client_for_provider(request, "codex").complete_with_tools(no_tools, model=route.resolved_model, reasoning_effort=route.reasoning_effort)
        else:
            result = await OpenAICompatibleProvider().complete_with_tools(no_tools, route.resolved_model)
        if result.get("tool_calls") or not isinstance(result.get("content"), str):
            raise GatewayError(502, "Il provider ha richiesto strumenti non autorizzati.", code="job_tool_not_allowed")
        return result["content"]
    provider = await _provider_for_route(request, route)
    return await provider.complete(payload, route.resolved_model)


async def _provider_stream(
    payload: ChatCompletionRequest,
    request: Request,
    route: ModelRoute,
) -> AsyncIterator[str]:
    provider = await _provider_for_route(request, route)
    session = getattr(request.state, "limit_session", None)
    stream = session.stream(payload, lambda current: provider.stream(current, route.resolved_model)) if session is not None else provider.stream(payload, route.resolved_model)
    async for chunk in stream:
        yield chunk


async def _stream_completion(
    *,
    payload: ChatCompletionRequest,
    request: Request,
    route: ModelRoute,
    request_id: str,
    api_key_id: int,
    prompt_tokens: int,
    started: float,
    response_model: str,
    capability_context: CapabilityContext,
    gateway_api_id: int | None,
) -> AsyncIterator[str]:
    created = int(time.time())
    chunks: list[str] = []
    status_code = 200
    error_code: str | None = None
    cancelled = False
    session = getattr(request.state, "limit_session", None)

    yield _sse(
        _stream_chunk(
            request_id,
            created,
            response_model,
            {"role": "assistant", "content": ""},
        )
    )

    try:
        if capability_context.has_mcp_tools and gateway_api_id is not None:
            result = await complete_with_mcp(
                payload=payload,
                context=capability_context,
                complete=lambda current: _provider_complete(
                    current,
                    request,
                    route,
                ),
                request_id=request_id,
                gateway_api_id=gateway_api_id,
            )
            prompt_tokens = result.prompt_tokens
            words = result.content.split(" ")
            for index, word in enumerate(words):
                content = f" {word}" if index else word
                chunks.append(content)
                yield _sse(
                    _stream_chunk(
                        request_id,
                        created,
                        response_model,
                        {"content": content},
                    )
                )
        else:
            async for content in _provider_stream(payload, request, route):
                chunks.append(content)
                yield _sse(
                    _stream_chunk(
                        request_id,
                        created,
                        response_model,
                        {"content": content},
                    )
                )
    except asyncio.CancelledError:
        cancelled = True
        status_code = 499
        error_code = "client_disconnected"
        raise
    except GatewayError as exc:
        status_code = exc.status_code
        error_code = exc.code
        yield _sse(
            openai_error_body(
                exc.message,
                error_type=exc.error_type,
                code=exc.code,
                param=exc.param,
            )
        )
    else:
        yield _sse(
            _stream_chunk(
                request_id,
                created,
                response_model,
                {},
                finish_reason="length" if session is not None and session.output_truncated else "stop",
            )
        )
    finally:
        if session is not None:
            prompt_tokens, completion_tokens = session.prompt_tokens, session.completion_tokens
        else:
            completion_tokens = count_text_tokens("".join(chunks))
        record_task = _record_usage_safely(
            request_id=request_id,
            api_key_id=api_key_id,
            route=route,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            status_code=status_code,
            started=started,
            error_code=error_code,
        )
        await asyncio.shield(record_task)

    if not cancelled and payload.stream_options and payload.stream_options.include_usage:
        yield _sse(
            {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": response_model,
                "choices": [],
                "usage": _usage(prompt_tokens, completion_tokens),
            }
        )
    if not cancelled:
        yield "data: [DONE]\n\n"


def _stream_chunk(
    request_id: str,
    created: int,
    model: str,
    delta: dict[str, object],
    *,
    finish_reason: str | None = None,
) -> dict[str, object]:
    chunk = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "logprobs": None,
                "finish_reason": finish_reason,
            }
        ],
    }
    if finish_reason is not None and deployments.selection():
        chunk["omniproxy"] = deployments.selection()
    return chunk


def _sse(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _usage(prompt_tokens: int, completion_tokens: int) -> dict[str, int]:
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _provider_error_response(
    exc: ProviderBrokerError,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "message": str(exc)},
        headers={"Cache-Control": "no-store"},
    )


def _invalid_auth_code_response(code: str) -> JSONResponse | None:
    if "\n" not in code and "\r" not in code and "\x00" not in code:
        return None
    return JSONResponse(
        status_code=400,
        content={
            "error": "invalid_auth_code",
            "message": "Il codice monouso non è valido.",
        },
        headers={"Cache-Control": "no-store"},
    )


async def _refresh_mcp_server(
    server: McpServerRecord,
) -> McpServerRecord:
    try:
        discovery = await McpGatewayClient().discover(server)
    except (ValueError, McpClientError) as exc:
        message = str(exc)
        refreshed = await save_mcp_server_discovery(
            server.id,
            status="error",
            status_message=message,
            protocol_version=None,
            server_info=None,
            tools=[],
        )
    except TimeoutError:
        refreshed = await save_mcp_server_discovery(
            server.id,
            status="error",
            status_message="Il server MCP non ha risposto entro il timeout.",
            protocol_version=None,
            server_info=None,
            tools=[],
        )
    except Exception as exc:  # noqa: BLE001 - normalizza errori SDK MCP
        logger.warning(
            "MCP discovery failed for server id=%s: %s",
            server.id,
            type(exc).__name__,
        )
        refreshed = await save_mcp_server_discovery(
            server.id,
            status="error",
            status_message="Il server non ha restituito una risposta MCP valida.",
            protocol_version=None,
            server_info=None,
            tools=[],
        )
    else:
        refreshed = await save_mcp_server_discovery(
            server.id,
            status="connected",
            status_message=None,
            protocol_version=discovery.protocol_version,
            server_info=discovery.server_info,
            tools=discovery.tools,
        )
    return refreshed or server


async def _model_catalog(app_instance: FastAPI) -> list[dict[str, object]]:
    ollama_result, codex_result, gemini_result, claude_result, upstream_result = (
        await asyncio.gather(
            _ollama_catalog(app_instance),
            _broker_catalog(
                "codex",
                CodexBrokerClient(app_instance.state.codex_broker_http),
            ),
            _broker_catalog(
                "gemini",
                AntigravityBrokerClient(
                    app_instance.state.gemini_broker_http
                ),
            ),
            _broker_catalog(
                "claude",
                ClaudeBrokerClient(app_instance.state.claude_broker_http),
            ),
            OpenAICompatibleProvider().catalog(),
        )
    )
    return [ollama_result, codex_result, gemini_result, claude_result, upstream_result]


async def _ollama_catalog(app_instance: FastAPI) -> dict[str, object]:
    discovered = await _discover_ollama(app_instance, timeout=3.0)
    if discovered is None:
        return {
            "id": "ollama",
            "name": "Ollama",
            "connected": False,
            "models": [],
            "message": "Nessun container Ollama raggiungibile.",
        }

    client, payload = discovered
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raw_models = []

    loaded_contexts = {}
    try:
        running = await client.get("/api/ps", timeout=2)
        running.raise_for_status()
        for item in running.json().get("models", []):
            if isinstance(item, dict) and isinstance(item.get("context_length"), int):
                loaded_contexts[item.get("name") or item.get("model")] = item["context_length"]
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        pass
    semaphore = asyncio.Semaphore(8)

    async def describe_model(
        item: object,
    ) -> dict[str, object] | None:
        if not isinstance(item, dict):
            return None
        model_id = item.get("name")
        if not isinstance(model_id, str) or not 1 <= len(model_id) <= 128:
            return None
        details = item.get("details")
        parameter_size = (
            details.get("parameter_size")
            if isinstance(details, dict)
            and isinstance(details.get("parameter_size"), str)
            else None
        )
        capabilities: list[str] = []
        reported_context = None
        try:
            async with semaphore:
                response = await client.post(
                    "/api/show",
                    json={"model": model_id},
                    timeout=4.0,
                )
                show = response.json()
            if response.is_success and isinstance(show, dict):
                raw_capabilities = show.get("capabilities")
                if isinstance(raw_capabilities, list):
                    capabilities = [
                        value
                        for value in raw_capabilities
                        if isinstance(value, str)
                    ]
                info = show.get("model_info") or {}
                if isinstance(info, dict):
                    value = info.get(str(info.get("general.architecture")) + ".context_length")
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 512:
                        reported_context = value
        except (httpx.HTTPError, ValueError):
            capabilities = []
        if capabilities and "completion" not in capabilities:
            return None
        efforts = (
            ["off", "on"] if "thinking" in capabilities else ["off"]
        )
        return {
            "id": model_id,
            "display_name": model_id,
            "description": (
                f"Modello locale · {parameter_size}"
                if parameter_size
                else "Modello disponibile nel container Ollama."
            ),
            "is_default": (
                model_id == settings.ollama_model
                or model_id.startswith(f"{settings.ollama_model}:")
            ),
            "reasoning_efforts": efforts,
            "default_reasoning_effort": (
                "on"
                if settings.ollama_think and "on" in efforts
                else "off"
            ),
            "reported_capabilities": capabilities,
            "capability_source": "ollama_api_show",
            "gateway_capabilities": {"text": "completion" in capabilities if capabilities else None, "images": "vision" in capabilities if capabilities else None, "client_tools": False, "structured_output": False},
            "context": {"reported_tokens": reported_context, "configured_tokens": settings.ollama_context_tokens, "loaded_tokens": loaded_contexts.get(model_id), "counting": "gateway_estimate"},
        }

    described = await asyncio.gather(
        *[describe_model(item) for item in raw_models[:100]]
    )
    models = [model for model in described if model is not None]
    models.sort(
        key=lambda model: (
            not bool(model["is_default"]),
            str(model["display_name"]).lower(),
        )
    )
    return {
        "id": "ollama",
        "name": "Ollama",
        "connected": True,
        "models": models,
    }


async def _broker_catalog(
    provider: str,
    client: (
        AntigravityBrokerClient
        | CodexBrokerClient
        | ClaudeBrokerClient
    ),
) -> dict[str, object]:
    names = {"codex": "Codex", "gemini": "Gemini", "claude": "Claude"}
    try:
        payload = await client.models()
    except ProviderBrokerError as exc:
        return {
            "id": provider,
            "name": names[provider],
            "connected": False,
            "models": [],
            "message": str(exc),
        }

    normalized_models: list[dict[str, object]] = []
    raw_models = payload.get("models")
    if isinstance(raw_models, list):
        for model in raw_models[:100]:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id")
            display_name = model.get("display_name")
            efforts = model.get("reasoning_efforts")
            default_effort = model.get("default_reasoning_effort")
            if (
                not isinstance(model_id, str)
                or not 1 <= len(model_id) <= 128
                or not isinstance(display_name, str)
                or not isinstance(efforts, list)
                or not all(
                    isinstance(effort, str) and 1 <= len(effort) <= 32
                    for effort in efforts
                )
                or not isinstance(default_effort, str)
                or default_effort not in efforts
            ):
                continue
            normalized_models.append(
                {
                    "id": model_id,
                    "display_name": display_name[:100],
                    "description": (
                        str(model.get("description", ""))[:300]
                    ),
                    "is_default": model.get("is_default") is True,
                    "reasoning_efforts": efforts,
                    "default_reasoning_effort": default_effort,
                }
            )
    result: dict[str, object] = {
        "id": provider,
        "name": names[provider],
        "connected": payload.get("connected") is True,
        "models": normalized_models,
    }
    return result


def _build_error(
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": code, "message": message},
        headers={"Cache-Control": "no-store"},
    )


def _set_build_activity(
    app_instance: FastAPI,
    project_id: int,
    *,
    role: str,
    phase: str,
) -> None:
    is_builder = role == "builder"
    app_instance.state.build_activities[project_id] = {
        "active": True,
        "role": role,
        "phase": phase,
        "title": (
            "Builder sta lavorando"
            if is_builder
            else "Analista sta lavorando"
        ),
        "message": (
            "Ha ricevuto la consegna e sta preparando la risposta."
            if is_builder and phase == "handoff"
            else (
                "Sta elaborando il progetto e lo storico della chat."
                if is_builder
                else "Sta analizzando la richiesta e preparando il brief."
            )
        ),
        "started_at": time.time(),
    }


def _clear_build_activity(
    app_instance: FastAPI,
    project_id: int,
) -> None:
    app_instance.state.build_activities.pop(project_id, None)


def _sanitize_build_files(
    snapshots: list[BuildFileSnapshot],
) -> list[dict[str, str]]:
    if len(snapshots) > BUILD_FILE_LIMIT:
        raise ValueError(
            f"La cartella può contenere al massimo {BUILD_FILE_LIMIT} "
            "file testuali indicizzati."
        )

    sanitized: list[dict[str, str]] = []
    seen: set[str] = set()
    total_bytes = 0
    for snapshot in snapshots:
        path = snapshot.path.replace("\\", "/")
        path_parts = path.split("/")
        lower_parts = [part.lower() for part in path_parts]
        file_name = lower_parts[-1]
        suffix = Path(file_name).suffix
        if any(part in BUILD_EXCLUDED_DIRECTORIES for part in lower_parts[:-1]):
            continue
        if (
            file_name == ".env"
            or file_name.startswith(".env.")
            or file_name in {
                ".npmrc",
                ".pypirc",
                "auth.json",
                "credentials.json",
                "service-account.json",
            }
            or suffix in {".key", ".p12", ".pem", ".pfx"}
        ):
            continue
        if path in seen:
            raise ValueError("La cartella contiene percorsi duplicati.")
        seen.add(path)

        encoded = snapshot.content.encode("utf-8")
        if len(encoded) > BUILD_FILE_BYTES_LIMIT:
            raise ValueError(
                f"Il file {path} supera il limite di 1 MB."
            )
        total_bytes += len(encoded)
        if total_bytes > BUILD_TOTAL_BYTES_LIMIT:
            raise ValueError(
                "Il contenuto testuale indicizzato supera il limite di 50 MB."
            )
        sanitized.append({"path": path, "content": snapshot.content})
    return sanitized


async def _validate_build_models(
    app_instance: FastAPI,
    *model_configs: BuildModelConfig,
    role_labels: tuple[str, ...] | None = None,
) -> JSONResponse | None:
    catalog = await _model_catalog(app_instance)
    labels = role_labels or (
        ("Analista idea", "Builder")
        if len(model_configs) == 2
        else ("modello Build",) * len(model_configs)
    )
    if len(labels) != len(model_configs):
        raise ValueError("Ogni modello Build deve avere un ruolo.")
    for role_label, model_config in zip(
        labels,
        model_configs,
        strict=True,
    ):
        role_reference = {
            "Analista idea": "all’Analista idea",
            "Builder": "al Builder",
            "modello Build": "al modello Build",
        }.get(role_label, f"al ruolo {role_label}")
        provider = next(
            (
                item
                for item in catalog
                if item.get("id") == model_config.provider
            ),
            None,
        )
        provider_name = (
            str(provider.get("name"))
            if provider is not None
            else model_config.provider.title()
        )
        if provider is None or provider.get("connected") is not True:
            return _build_error(
                409,
                "build_provider_not_connected",
                (
                    f"Il provider {provider_name} assegnato {role_reference} "
                    "non è collegato. Ricollegalo oppure scegli un provider "
                    "disponibile."
                ),
            )
        model = next(
            (
                item
                for item in provider.get("models", [])
                if isinstance(item, dict)
                and item.get("id") == model_config.model
            ),
            None,
        )
        if model is None:
            return _build_error(
                400,
                "build_model_not_available",
                (
                    f"Il modello {model_config.model} assegnato "
                    f"{role_reference} non è disponibile su {provider_name}."
                ),
            )
        if model_config.reasoning_effort not in model.get(
            "reasoning_efforts",
            [],
        ):
            return _build_error(
                400,
                "build_reasoning_not_supported",
                (
                    f"Il reasoning {model_config.reasoning_effort} non è "
                    f"supportato dal modello assegnato {role_reference}."
                ),
            )
    return None


async def _build_model_route(
    app_instance: FastAPI,
    model_config: BuildModelConfig,
    *,
    role_label: str = "modello Build",
) -> ModelRoute:
    invalid = await _validate_build_models(
        app_instance,
        model_config,
        role_labels=(role_label,),
    )
    if invalid is not None:
        try:
            payload = json.loads(invalid.body)
        except (TypeError, ValueError):
            payload = {}
        raise GatewayError(
            invalid.status_code,
            str(
                payload.get(
                    "message",
                    "Il modello selezionato non è disponibile.",
                )
            ),
            code=str(payload.get("error", "build_model_unavailable")),
        )
    return ModelRoute(
        requested_model=f"build:{model_config.provider}:{model_config.model}",
        provider=model_config.provider,
        resolved_model=model_config.model,
        reasoning_effort=model_config.reasoning_effort,
    )


async def _build_complete(
    request: Request,
    *,
    model_config: BuildModelConfig,
    system_prompt: str,
    user_prompt: str,
    output_tokens: int = 2200,
    role_label: str = "modello Build",
) -> str:
    route = await _build_model_route(
        request.app,
        model_config,
        role_label=role_label,
    )
    payload = ChatCompletionRequest(
        model=route.requested_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        stream=False,
        max_completion_tokens=output_tokens,
    )
    return await _provider_complete(payload, request, route)


def _trim_build_text(value: str, limit: int = 14_000) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n\n[Contenuto abbreviato da OmniProxy]"


_ANALYST_PLAN_PATTERN = re.compile(
    r"<omniproxy-plan>\s*(\{.*?\})\s*</omniproxy-plan>",
    re.DOTALL,
)


def _extract_analyst_plan(
    content: str,
) -> tuple[str, list[dict[str, str]]]:
    match = _ANALYST_PLAN_PATTERN.search(content)
    clean_content = _ANALYST_PLAN_PATTERN.sub("", content).strip()
    if match is None:
        return clean_content, []
    try:
        payload = json.loads(match.group(1))
    except (TypeError, ValueError):
        return clean_content, []
    raw_phases = payload.get("phases") if isinstance(payload, dict) else None
    if not isinstance(raw_phases, list):
        return clean_content, []
    phases: list[dict[str, str]] = []
    for item in raw_phases[:12]:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title", "")).strip().split())[:120]
        instruction = str(
            item.get("instruction", item.get("summary", ""))
        ).strip()[:2400]
        if title and instruction:
            phases.append({"title": title, "instruction": instruction})
    return clean_content, phases


def _analyst_mode_instruction(analyst_mode: str) -> str:
    if analyst_mode == "schematic":
        return (
            "MODALITÀ SCHEMATICA: usa frasi brevi e un massimo di 6 punti. "
            "Non scrivere codice, pseudocodice, patch o una soluzione tecnica "
            "dettagliata. Non decidere al posto del Builder come implementare: "
            "spiega soltanto cosa deve cambiare, perché e come verificare il "
            "risultato."
        )
    return (
        "MODALITÀ DETTAGLIATA: chiarisci requisiti, alternative, rischi e "
        "criteri di accettazione, senza scrivere codice né applicare modifiche."
    )


def _analyst_plan_protocol() -> str:
    return (
        "Se la richiesta descrive un lavoro eseguibile dal Builder, chiudi "
        "la risposta con un solo blocco macchina "
        '<omniproxy-plan>{"phases":[{"title":"Titolo breve",'
        '"instruction":"Cosa cambiare e come verificarlo"}]}'
        "</omniproxy-plan>. Crea da 1 a 8 fasi nell’ordine corretto; ogni "
        "istruzione deve essere autonoma, breve e riguardare solo quella fase. "
        "Il blocco non è testo per l’utente: non commentarlo. Se la richiesta "
        "è solo una domanda e non richiede lavoro, omettilo."
    )


async def _run_build_planning_pipeline(
    request: Request,
    *,
    analyst: BuildModelConfig,
    analyst_mode: str,
    idea: str,
    project_context: str,
) -> list[tuple[str, str]]:
    context = (
        project_context
        if project_context
        else "Nessuna cartella progetto è stata ancora indicizzata."
    )
    mode_instruction = _analyst_mode_instruction(analyst_mode)
    concise = analyst_mode == "schematic"
    analysis = await _build_complete(
        request,
        model_config=analyst,
        system_prompt=(
            "Sei l'Analista Idea di OmniProxy Build. Devi capire il problema "
            "prima di proporre una soluzione. Non scrivere codice e non "
            "inventare requisiti. Rispondi in italiano con sezioni brevi. "
            f"{mode_instruction}"
        ),
        user_prompt=(
            "FASE 1 — COMPRENSIONE\n\n"
            f"Idea dell'utente:\n{idea}\n\n"
            f"Snapshot del progetto:\n{context}\n\n"
            "Identifica obiettivo, utenti, flusso principale, vincoli, "
            "assunzioni da validare, rischi e criteri di successo. Distingui "
            "ciò che è certo da ciò che richiede conferma."
        ),
        output_tokens=650 if concise else 2200,
        role_label="Analista idea",
    )

    builder_brief = await _build_complete(
        request,
        model_config=analyst,
        system_prompt=(
            "Sei il Prompt Architect di OmniProxy Build. Trasformi un'idea "
            "analizzata in un brief tecnico preciso per un modello Builder. "
            "Non eseguire il lavoro e non comprimere tutto in un solo prompt. "
            f"{mode_instruction}"
        ),
        user_prompt=(
            "FASE 2 — BRIEF PER IL BUILDER\n\n"
            f"Idea originale:\n{idea}\n\n"
            f"Analisi precedente:\n{_trim_build_text(analysis)}\n\n"
            "Scrivi un brief operativo: risultato atteso, perimetro, "
            "architettura, vincoli, sicurezza, dati, UX, test e definition "
            "of done. Evidenzia le decisioni ancora aperte."
        ),
        output_tokens=700 if concise else 2200,
        role_label="Analista idea",
    )

    roadmap = await _build_complete(
        request,
        model_config=analyst,
        system_prompt=(
            "Sei il Planner di OmniProxy Build. Crei una sequenza incrementale "
            "verificabile. Ogni fase deve essere eseguibile separatamente da "
            "un modello Builder e non deve anticipare lavoro delle fasi "
            f"successive. {mode_instruction}"
        ),
        user_prompt=(
            "FASE 3 — ROADMAP MULTI-FASE\n\n"
            f"Brief:\n{_trim_build_text(builder_brief)}\n\n"
            "Dividi il lavoro in 1-8 fasi. Per ogni fase indica soltanto "
            "obiettivo, cambi richiesti e controllo di accettazione. Non "
            "creare un unico prompt cumulativo per il Builder.\n\n"
            f"{_analyst_plan_protocol()}"
        ),
        output_tokens=900 if concise else 3200,
        role_label="Analista idea",
    )

    future_features = await _build_complete(
        request,
        model_config=analyst,
        system_prompt=(
            "Sei il Product Strategist di OmniProxy Build. Suggerisci "
            "estensioni future solo dopo aver protetto l'MVP da scope creep. "
            f"{mode_instruction}"
        ),
        user_prompt=(
            "FASE 4 — EVOLUZIONI FUTURE\n\n"
            f"Idea:\n{idea}\n\n"
            f"Roadmap MVP:\n{_trim_build_text(roadmap)}\n\n"
            "Proponi funzionalità future ordinate per impatto e costo. Per "
            "ciascuna spiega valore, prerequisiti, rischi e momento corretto "
            "per introdurla. Separa chiaramente quick win, prossima release "
            "e idee sperimentali."
        ),
        output_tokens=600 if concise else 2200,
        role_label="Analista idea",
    )
    return [
        ("analysis", analysis),
        ("builder_brief", builder_brief),
        ("roadmap", roadmap),
        ("future_features", future_features),
    ]


def _requests_builder_handoff(message: str) -> bool:
    normalized = " ".join(message.casefold().split())
    if "builder" not in normalized:
        return False
    action = (
        r"(?:pass\w*|inoltr\w*|consegn\w*|mand\w*|trasfer\w*|"
        r"attiv\w*|avvi\w*|proced\w*)"
    )
    if re.search(
        rf"\b(?:non|senza)\s+(?:\w+\s+){{0,2}}{action}\b",
        normalized,
    ):
        return False
    return bool(re.search(rf"\b{action}\b", normalized))


_BUILDER_CHANGES_PATTERN = re.compile(
    r"<omniproxy-changes>\s*(\{.*?\})\s*</omniproxy-changes>",
    re.DOTALL,
)
_BUILDER_COMMANDS_PATTERN = re.compile(
    r"<omniproxy-commands>\s*(\{.*?\})\s*</omniproxy-commands>",
    re.DOTALL,
)
_ALLOWED_MAINTENANCE_COMMANDS = {
    "docker compose up -d --build --force-recreate",
    "docker compose ps",
}
_UNIFIED_HUNK_PATTERN = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)


def _builder_display_content(content: str) -> str:
    without_changes = _BUILDER_CHANGES_PATTERN.sub("", content)
    without_commands = _BUILDER_COMMANDS_PATTERN.sub("", without_changes)
    return _ANALYST_PLAN_PATTERN.sub("", without_commands).strip()


def _parse_builder_commands(
    content: str,
) -> tuple[list[str], str | None]:
    match = _BUILDER_COMMANDS_PATTERN.search(content)
    if match is None:
        return [], None
    try:
        payload = json.loads(match.group(1))
    except (TypeError, ValueError):
        return [], "Il blocco comandi del Builder non contiene JSON valido."
    raw_commands = payload.get("commands") if isinstance(payload, dict) else None
    if not isinstance(raw_commands, list) or not raw_commands:
        return [], "Il blocco comandi del Builder è vuoto."
    commands: list[str] = []
    for value in raw_commands:
        command = " ".join(str(value).strip().split())
        if command not in _ALLOWED_MAINTENANCE_COMMANDS:
            return [], f"Comando Builder non consentito: {command}"
        if command not in commands:
            commands.append(command)
    return commands, None


def _collect_maintenance_commands(
    contents: list[str],
) -> tuple[list[str], str | None]:
    """Collect allowlisted commands from every phase, preserving their order."""
    collected: list[str] = []
    for content in contents:
        commands, error = _parse_builder_commands(content)
        if error is not None:
            return [], error
        for command in commands:
            if command not in collected:
                collected.append(command)
    return collected, None


async def _submit_maintenance_job(
    request: Request,
    project: dict[str, object],
    commands: list[str],
) -> dict[str, object] | None:
    if not commands:
        return None
    if any(command not in _ALLOWED_MAINTENANCE_COMMANDS for command in commands):
        return {
            "status": "rejected",
            "message": "Il comando Docker richiesto non è consentito.",
        }
    if str(project.get("folder_name", "")) != (
        settings.maintenance_workspace_name
    ):
        return {
            "status": "rejected",
            "message": (
                "Il runner Docker è vincolato alla cartella OmniProxy "
                "configurata e non può operare su questo progetto."
            ),
        }
    try:
        response = await request.app.state.maintenance_http.post(
            "/v1/commands",
            json={"commands": commands},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return {
            "status": "unavailable",
            "message": (
                "Il runner locale dei comandi non è disponibile; le patch "
                "restano applicate ma il container non è stato aggiornato."
            ),
        }
    return payload if isinstance(payload, dict) else None


async def _submit_maintenance_commands(
    request: Request,
    project: dict[str, object],
    content: str,
) -> dict[str, object] | None:
    commands, error = _parse_builder_commands(content)
    if error is not None:
        return {"status": "rejected", "message": error}
    return await _submit_maintenance_job(request, project, commands)


def _legacy_readonly_builder_answer(content: str) -> bool:
    normalized = " ".join(content.casefold().split())
    return (
        "indicizzata in sola lettura" in normalized
        or "cartella è indicizzata in sola lettura" in normalized
        or "non dichiaro modifiche applicate" in normalized
        or "file troncato" in normalized
        or "non sono inclusi nello snapshot" in normalized
        or "non è presente nello snapshot" in normalized
        or "mancano i contenuti completi" in normalized
        or "mancano i contenuti di" in normalized
        or "manca il contenuto di" in normalized
        or "non posso preparare patch" in normalized
    )


def _parse_builder_patches(
    content: str,
) -> tuple[list[dict[str, str]], str | None]:
    match = _BUILDER_CHANGES_PATTERN.search(content)
    if match is None:
        return [], None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return [], (
            "Il Builder ha prodotto una patch non valida. Chiedigli di "
            "rigenerarla senza abbreviare il blocco omniproxy-changes."
        )
    patches = payload.get("patches") if isinstance(payload, dict) else None
    if not isinstance(patches, list) or not 1 <= len(patches) <= 12:
        return [], "La proposta Builder deve contenere da 1 a 12 patch."

    parsed: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in patches:
        if not isinstance(item, dict):
            return [], "Una patch Builder non ha una struttura valida."
        raw_path = item.get("path")
        diff = item.get("diff")
        if not isinstance(raw_path, str) or not isinstance(diff, str):
            return [], "Ogni patch Builder deve indicare path e diff."
        try:
            snapshot = BuildFileSnapshot(path=raw_path, content="")
            sanitized = _sanitize_build_files([snapshot])
        except ValueError as exc:
            return [], str(exc)
        if not sanitized:
            return [], (
                f"Il Builder ha proposto un file non consentito: {raw_path}."
            )
        path = sanitized[0]["path"]
        if path in seen:
            return [], f"Il Builder ha duplicato la patch per {path}."
        if len(diff) > 100_000:
            return [], f"La patch per {path} supera il limite consentito."
        seen.add(path)
        parsed.append({"path": path, "diff": diff})
    return parsed, None


def _find_patch_segment(
    lines: list[str],
    segment: list[str],
    *,
    start: int,
    expected: int,
) -> int | None:
    if not segment:
        return min(max(expected, start), len(lines))
    candidates: list[int] = []
    upper = len(lines) - len(segment) + 1
    for index in range(max(0, start), max(0, upper)):
        if lines[index:index + len(segment)] == segment:
            candidates.append(index)
    if not candidates:
        normalized_segment = [line.strip() for line in segment]
        normalized_candidates: list[int] = []
        for index in range(max(0, start), max(0, upper)):
            window = lines[index:index + len(segment)]
            if [line.strip() for line in window] == normalized_segment:
                normalized_candidates.append(index)
        if normalized_candidates:
            return min(
                normalized_candidates,
                key=lambda index: abs(index - expected),
            )

        fuzzy: list[tuple[float, int]] = []
        lower_bound = max(start, expected - 120)
        upper_bound = min(max(0, upper), expected + 121)
        expected_text = "\n".join(normalized_segment)
        for index in range(max(0, lower_bound), max(0, upper_bound)):
            window = lines[index:index + len(segment)]
            score = SequenceMatcher(
                None,
                expected_text,
                "\n".join(line.strip() for line in window),
                autojunk=False,
            ).ratio()
            fuzzy.append((score, index))
        fuzzy.sort(reverse=True)
        if fuzzy and fuzzy[0][0] >= 0.94:
            if len(fuzzy) == 1 or fuzzy[0][0] - fuzzy[1][0] >= 0.02:
                return fuzzy[0][1]
        return None
    return min(candidates, key=lambda index: abs(index - expected))


def _find_unique_patch_core(
    lines: list[str],
    segment: list[str],
) -> int | None:
    """Locate a changed core only when it identifies one unambiguous place."""
    if not segment:
        return None
    exact = [
        index
        for index in range(len(lines) - len(segment) + 1)
        if lines[index:index + len(segment)] == segment
    ]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    normalized = [line.strip() for line in segment]
    whitespace_matches = [
        index
        for index in range(len(lines) - len(segment) + 1)
        if [
            line.strip()
            for line in lines[index:index + len(segment)]
        ] == normalized
    ]
    return whitespace_matches[0] if len(whitespace_matches) == 1 else None


def _apply_unified_diff(original: str, diff: str, *, path: str) -> str:
    current = original.replace("\r\n", "\n").split("\n")
    patch_lines = diff.replace("\r\n", "\n").split("\n")
    index = 0
    offset = 0
    hunks = 0
    while index < len(patch_lines):
        header = _UNIFIED_HUNK_PATTERN.match(patch_lines[index])
        if header is None:
            index += 1
            continue
        hunks += 1
        old_start = int(header.group(1))
        old_count = int(header.group(2) or "1")
        new_count = int(header.group(4) or "1")
        index += 1
        old_segment: list[str] = []
        new_segment: list[str] = []
        entries: list[tuple[str, str]] = []
        while index < len(patch_lines):
            line = patch_lines[index]
            if _UNIFIED_HUNK_PATTERN.match(line):
                break
            if line.startswith(("diff --git ", "--- ", "+++ ")):
                break
            if line == r"\ No newline at end of file":
                index += 1
                continue
            if not line:
                if index == len(patch_lines) - 1:
                    break
                raise ValueError(
                    f"Patch non valida per {path}: riga hunk senza prefisso."
                )
            prefix, value = line[0], line[1:]
            if prefix == " ":
                entries.append((prefix, value))
                old_segment.append(value)
                new_segment.append(value)
            elif prefix == "-":
                entries.append((prefix, value))
                old_segment.append(value)
            elif prefix == "+":
                entries.append((prefix, value))
                new_segment.append(value)
            else:
                break
            index += 1
        # I modelli possono sbagliare i conteggi dell'header pur producendo
        # un hunk coerente. Le righe effettive vengono quindi ricontate,
        # mentre il contenuto deve comunque combaciare con lo snapshot.
        expected = max(0, old_start - 1 + offset)
        location = _find_patch_segment(
            current,
            old_segment,
            start=0,
            expected=expected,
        )
        if location is None:
            changed_indexes = [
                position
                for position, (prefix, _value) in enumerate(entries)
                if prefix != " "
            ]
            if changed_indexes:
                core = entries[
                    changed_indexes[0]:changed_indexes[-1] + 1
                ]
                core_old = [
                    value for prefix, value in core if prefix != "+"
                ]
                core_new = [
                    value for prefix, value in core if prefix != "-"
                ]
                core_location = _find_unique_patch_core(current, core_old)
                if core_location is not None:
                    location = core_location
                    old_segment = core_old
                    new_segment = core_new
            if location is None:
                raise ValueError(
                    f"La patch per {path} non corrisponde allo snapshot corrente."
                )
        current[location:location + len(old_segment)] = new_segment
        offset += len(new_segment) - len(old_segment)
    if hunks == 0:
        raise ValueError(f"La patch per {path} non contiene hunk applicabili.")
    return "\n".join(current)


async def _materialize_builder_changes(
    project_id: int,
    content: str,
) -> tuple[list[dict[str, object]], str | None]:
    patches, error = _parse_builder_patches(content)
    if error is not None or not patches:
        return [], error
    files = await list_build_project_file_contents(project_id)
    by_path = {str(item["path"]): item for item in files}
    changes: list[dict[str, object]] = []
    proposed_sizes = {
        str(item["path"]): int(item["size_bytes"])
        for item in files
    }
    for patch in patches:
        path = patch["path"]
        current = by_path.get(path)
        original = str(current["content"]) if current is not None else ""
        try:
            updated = _apply_unified_diff(
                original,
                patch["diff"],
                path=path,
            )
        except ValueError as exc:
            return [], str(exc)
        encoded = updated.encode("utf-8")
        if len(encoded) > BUILD_FILE_BYTES_LIMIT:
            return [], f"Il file risultante {path} supera il limite di 1 MB."
        proposed_sizes[path] = len(encoded)
        changes.append(
            {
                "path": path,
                "content": updated,
                "base_sha256": (
                    str(current["content_sha256"])
                    if current is not None
                    else None
                ),
                "result_sha256": hashlib.sha256(encoded).hexdigest(),
                "operation": "update" if current is not None else "create",
            }
        )
    if sum(proposed_sizes.values()) > BUILD_TOTAL_BYTES_LIMIT:
        return [], "Le modifiche superano il limite snapshot di 50 MB."
    return changes, None


async def _targeted_builder_context(
    project_id: int,
    request_text: str,
    *,
    fallback_texts: list[str] | None = None,
) -> str:
    files = await list_build_project_files(project_id)
    by_path = {
        str(item["path"]).replace("\\", "/"): item
        for item in files
    }
    basename_counts: dict[str, int] = {}
    for item in files:
        basename = str(item["path"]).replace("\\", "/").split("/")[-1].casefold()
        basename_counts[basename] = basename_counts.get(basename, 0) + 1

    def referenced_paths(text: str) -> list[str]:
        normalized = text.replace("\\", "/").casefold()
        matched: list[str] = []
        for item in files:
            path = str(item["path"]).replace("\\", "/")
            folded_path = path.casefold()
            basename = folded_path.split("/")[-1]
            if (
                folded_path in normalized
                or (
                    len(basename) >= 5
                    and basename_counts.get(basename) == 1
                    and re.search(
                        rf"(?<![\w.-]){re.escape(basename)}(?![\w.-])",
                        normalized,
                    )
                )
            ):
                matched.append(path)
        return matched

    normalized_request = "\n".join(
        [request_text, *(fallback_texts or [])]
    ).casefold()
    preferred = referenced_paths(request_text)
    if not preferred:
        for fallback in fallback_texts or []:
            preferred = referenced_paths(fallback)
            if preferred:
                break
    heuristic_groups = (
        (
            ("sqlite", "database", "persist", "tabella"),
            ("app/database.py",),
        ),
        (
            ("endpoint", "route", "rotta", "gateway", "api "),
            ("app/main.py", "app/schemas.py"),
        ),
        (
            ("auth", "chiav", "token", "bearer", "paused", "revoc"),
            (
                "app/auth.py",
                "app/database.py",
                "tests/test_gateway.py",
                "tests/test_managed_apis.py",
            ),
        ),
        (
            ("dashboard", "interfaccia", "pulsante", "ui", "frontend"),
            (
                "app/templates/dashboard.html",
                "app/static/dashboard.css",
                "app/static/dashboard.js",
            ),
        ),
        (
            ("docker", "container", "compose", "rebuild"),
            (
                "docker-compose.yml",
                "Dockerfile",
                "maintenance-runner/Dockerfile",
                "maintenance-runner/server.py",
            ),
        ),
    )
    for keywords, candidates in heuristic_groups:
        if any(keyword in normalized_request for keyword in keywords):
            preferred.extend(
                path for path in candidates if path in by_path
            )
    if any(
        keyword in normalized_request
        for keyword in ("test", "pytest", "verifica automat")
    ):
        preferred.extend(
            path for path in by_path if path.startswith("tests/")
        )
    preferred = list(dict.fromkeys(preferred))
    preferred_bytes = sum(
        int(by_path[path]["size_bytes"])
        for path in preferred
        if path in by_path
    )
    preferred_budget = min(
        2_500_000,
        max(240_000, preferred_bytes + 100_000),
    )
    return await build_project_context(
        project_id,
        max_characters=preferred_budget if preferred else 120_000,
        preferred_paths=preferred,
        preferred_only=bool(preferred),
    )


async def _run_builder_with_context_recovery(
    request: Request,
    *,
    project: dict[str, object],
    model_config: BuildModelConfig,
    history: list[dict[str, object]],
    artifacts: list[dict[str, object]],
    project_context: str,
    request_text: str,
) -> str:
    """Retry transparently when the Builder asks for indexed file contents."""
    current_context = project_context
    recovery_requests: list[str] = [request_text]
    retry_history = list(history)
    answer = ""
    for attempt in range(4):
        answer = await _run_build_chat(
            request,
            project=project,
            model_config=model_config,
            lane="builder",
            history=retry_history,
            artifacts=artifacts,
            project_context=current_context,
        )
        if not _legacy_readonly_builder_answer(answer):
            return answer
        recovery_requests.append(_builder_display_content(answer))
        recovered_context = await _targeted_builder_context(
            int(project["id"]),
            "\n".join(recovery_requests),
        )
        if recovered_context == current_context:
            recovered_context = await build_project_context(
                int(project["id"]),
                max_characters=2_500_000,
            )
        if attempt == 3:
            break
        current_context = recovered_context
        retry_history = [
            *history,
            {
                "lane": "builder",
                "role": "user",
                "message_type": "chat",
                "content": (
                    "OmniProxy ha caricato automaticamente dall’indice i "
                    "file richiesti. Non chiedere una nuova sincronizzazione: "
                    "usa lo snapshot aggiornato e produci ora la patch "
                    "verificabile per la fase corrente."
                ),
            },
        ]
    return answer


async def _run_builder_phase(
    request: Request,
    *,
    project: dict[str, object],
    phase: dict[str, object],
    phases: list[dict[str, object]],
    instruction: str = "",
) -> dict[str, object]:
    project_id = int(project["id"])
    phase_id = int(phase["id"])
    position = int(phase["position"])
    title = str(phase["title"])
    phase_instruction = _trim_build_text(str(phase["instruction"]), 2400)
    user_note = _trim_build_text(instruction.strip(), 2400)
    source_value = phase.get("source_message_id")
    source_id = int(source_value) if isinstance(source_value, int) else None
    handoff_content = (
        f"## Fase {position} di {len(phases)} · {title}\n\n"
        f"{phase_instruction}\n\n"
        "Esegui esclusivamente questa fase. Non anticipare le successive. "
        "Concludi con una patch verificabile per i soli file di questa fase."
    )
    if user_note:
        handoff_content += f"\n\nNota breve dell’utente: {user_note}"
    delivered = await add_build_message(
        project_id,
        lane="builder",
        role="user",
        content=handoff_content,
        message_type="handoff",
        source_message_id=source_id,
    )
    project_context = await _targeted_builder_context(
        project_id,
        f"{title}\n{phase_instruction}\n{user_note}",
    )
    messages = await list_build_messages(project_id, limit=100)
    history = [
        message
        for message in messages
        if message["lane"] == "builder"
    ][-12:]
    builder = BuildModelConfig.model_validate(project["builder"])
    _set_build_activity(
        request.app,
        project_id,
        role="builder",
        phase="chain",
    )
    try:
        answer = await _run_builder_with_context_recovery(
            request,
            project=project,
            model_config=builder,
            history=history,
            # La coda invia solo la fase corrente: il vecchio brief
            # cumulativo e le fasi future non vengono inclusi nel prompt.
            artifacts=[],
            project_context=project_context,
            request_text=f"{title}\n{phase_instruction}\n{user_note}",
        )
    except BaseException as exc:
        await set_build_phase_builder_result(
            project_id,
            phase_id,
            builder_message_id=None,
            status="blocked",
            error=str(exc)[:800],
        )
        raise
    saved = await add_build_message(
        project_id,
        lane="builder",
        role="assistant",
        content=answer,
    )
    changes, change_error = await _materialize_builder_changes(
        project_id,
        answer,
    )
    blocked_message = change_error
    if not changes and blocked_message is None:
        blocked_message = (
            "Il Builder non ha prodotto una patch verificabile per questa "
            "fase. La catena è in pausa: chiarisci la fase con l’Analista "
            "prima di proseguire."
        )
    phase_status = "awaiting_apply" if changes else "blocked"
    updated_phase = await set_build_phase_builder_result(
        project_id,
        phase_id,
        builder_message_id=int(saved["id"]),
        status=phase_status,
        error=blocked_message,
    )
    return {
        "status": "completed" if changes else "blocked",
        "handoff_message": delivered,
        "builder_message": saved,
        "phase": updated_phase or phase,
        "changes": changes,
        "change_error": blocked_message,
        "phases": await list_build_phases(project_id),
    }


async def _advance_builder_chain(
    request: Request,
    *,
    project: dict[str, object],
    instruction: str = "",
) -> dict[str, object]:
    project_id = int(project["id"])
    phases = await list_build_phases(project_id)
    phase, claimed = await claim_next_build_phase(project_id)
    if phase is None:
        return {
            "status": "chain_completed",
            "message": "Tutte le fasi della checklist sono completate.",
            "phases": phases,
            "changes": [],
            "change_error": None,
        }
    if not claimed:
        phase_status = str(phase["status"])
        builder_message = None
        changes: list[dict[str, object]] = []
        change_error = (
            str(phase["error"]) if phase.get("error") is not None else None
        )
        builder_message_id = phase.get("builder_message_id")
        if isinstance(builder_message_id, int):
            messages = await list_build_messages(project_id, limit=100)
            builder_message = next(
                (
                    message
                    for message in messages
                    if int(message["id"]) == builder_message_id
                ),
                None,
            )
            if builder_message is not None and phase_status == "awaiting_apply":
                changes, change_error = await _materialize_builder_changes(
                    project_id,
                    str(builder_message["content"]),
                )
        return {
            "status": (
                "already_completed"
                if phase_status == "awaiting_apply"
                else phase_status
            ),
            "phase": phase,
            "builder_message": builder_message,
            "phases": phases,
            "changes": changes,
            "change_error": change_error,
        }
    return await _run_builder_phase(
        request,
        project=project,
        phase=phase,
        phases=phases,
        instruction=instruction,
    )


async def _run_builder_handoff(
    request: Request,
    *,
    project: dict[str, object],
    source_message: dict[str, object],
    instruction: str,
    artifacts: list[dict[str, object]],
    project_context: str,
) -> dict[str, object]:
    if await list_build_phases(int(project["id"])):
        return await _advance_builder_chain(
            request,
            project=project,
            instruction=instruction,
        )
    source_id_value = source_message.get("id")
    source_id = (
        int(source_id_value) if isinstance(source_id_value, int) else None
    )
    source_content = _trim_build_text(
        str(source_message.get("content", "")),
        14_000,
    )
    clean_instruction = _trim_build_text(
        instruction.strip()
        or (
            "Prendi in carico il brief, verifica le decisioni aperte e "
            "avvia la prima fase eseguibile."
        ),
        4_000,
    )
    handoff_content = (
        "## Consegna strutturata dall’Analista Idea\n\n"
        f"{source_content}\n\n"
        "## Indicazione dell’utente\n\n"
        f"{clean_instruction}"
    )
    project_context = await _targeted_builder_context(
        int(project["id"]),
        f"{source_content}\n{clean_instruction}",
    )

    messages = await list_build_messages(int(project["id"]), limit=100)
    existing = next(
        (
            message
            for message in messages
            if message["lane"] == "builder"
            and message.get("message_type") == "handoff"
            and message.get("source_message_id") == source_id
            and message["content"] == handoff_content
        ),
        None,
    )
    if existing is None:
        delivered = await add_build_message(
            int(project["id"]),
            lane="builder",
            role="user",
            content=handoff_content,
            message_type="handoff",
            source_message_id=source_id,
        )
        messages.append(delivered)
    else:
        delivered = existing
        previous_answer = next(
            (
                message
                for message in reversed(messages)
                if message["lane"] == "builder"
                and message["role"] == "assistant"
                and int(message["id"]) > int(delivered["id"])
            ),
            None,
        )
        if previous_answer is not None:
            changes, change_error = await _materialize_builder_changes(
                int(project["id"]),
                str(previous_answer["content"]),
            )
            if not _legacy_readonly_builder_answer(
                str(previous_answer["content"])
            ):
                return {
                    "status": "already_completed",
                    "handoff_message": delivered,
                    "builder_message": previous_answer,
                    "changes": changes,
                    "change_error": change_error,
                }

    history = [
        message
        for message in messages
        if message["lane"] == "builder"
    ][-12:]
    builder = BuildModelConfig.model_validate(project["builder"])
    project_id = int(project["id"])
    _set_build_activity(
        request.app,
        project_id,
        role="builder",
        phase="handoff",
    )
    try:
        answer = await _run_builder_with_context_recovery(
            request,
            project=project,
            model_config=builder,
            history=history,
            artifacts=artifacts,
            project_context=project_context,
            request_text=f"{source_content}\n{clean_instruction}",
        )
    except BaseException:
        _clear_build_activity(request.app, project_id)
        raise
    saved = await add_build_message(
        int(project["id"]),
        lane="builder",
        role="assistant",
        content=answer,
    )
    changes, change_error = await _materialize_builder_changes(
        int(project["id"]),
        answer,
    )
    return {
        "status": "completed",
        "handoff_message": delivered,
        "builder_message": saved,
        "changes": changes,
        "change_error": change_error,
    }


async def _run_build_chat(
    request: Request,
    *,
    project: dict[str, object],
    model_config: BuildModelConfig,
    lane: str,
    history: list[dict[str, object]],
    artifacts: list[dict[str, object]],
    project_context: str,
) -> str:
    if lane == "builder":
        latest_handoff = next(
            (
                index
                for index in range(len(history) - 1, -1, -1)
                if history[index].get("message_type") == "handoff"
            ),
            None,
        )
        if latest_handoff is not None:
            history = history[latest_handoff:]
        history = [
            item
            for item in history
            if not (
                item.get("role") == "assistant"
                and _legacy_readonly_builder_answer(
                    str(item.get("content", ""))
                )
            )
        ]
    artifact_context = "\n\n".join(
        f"## {item['artifact_type']}\n{_trim_build_text(str(item['content']), 8000)}"
        for item in artifacts
    )
    role_prompt = (
        "Sei l'Analista Idea. Aiuta a chiarire requisiti, alternative e "
        "roadmap. Non affermare di aver modificato file. Se l’utente chiede "
        "di passare il lavoro al Builder, prepara una consegna operativa: "
        "l’orchestratore la inoltrerà realmente dopo la tua risposta. "
        f"{_analyst_mode_instruction(str(project['analyst_mode']))} "
        f"{_analyst_plan_protocol()}"
        if lane == "analyst"
        else (
            "Sei il modello Builder. Lavora su una sola fase alla volta, "
            "fornisci modifiche concrete e verifiche. Un messaggio "
            "HANDOFF DALL’ANALISTA è una consegna reale: conferma di averla "
            "ricevuta ed esegui la prima fase richiesta usando lo snapshot. "
            "Quando il lavoro richiede file, produci patch unified diff "
            "applicabili: non limitarti a descrivere cosa dovrebbe fare un "
            "altro Builder. Dopo un riepilogo breve aggiungi ESATTAMENTE un "
            "blocco <omniproxy-changes> contenente JSON valido nella forma "
            "{\"patches\":[{\"path\":\"percorso/relativo\","
            "\"diff\":\"--- a/percorso/relativo\\n+++ "
            "b/percorso/relativo\\n@@ -1,1 +1,1 @@\\n..."
            "\"}]}</omniproxy-changes>. Nel valore diff codifica gli a capo "
            "come \\n, usa hunk standard con conteggi corretti e almeno tre "
            "righe di contesto. Includi solo i file realmente modificati, "
            "non usare percorsi assoluti e non proporre segreti o file .env. "
            "Per creare un file usa un hunk da -0,0. Non eliminare file. "
            "Se un file necessario è marcato FILE TRONCATO o manca, indica "
            "con precisione i relativi percorsi: OmniProxy recupererà i "
            "contenuti dal proprio indice e rilancerà automaticamente questa "
            "fase. Non chiedere all’utente di sincronizzare file già elencati "
            "nel MANIFEST. Non dire "
            "che i file sono già stati scritti: comunica che la proposta è "
            "pronta per la verifica e l’applicazione sicura di OmniProxy. "
            "Lo Snapshot file del messaggio corrente è la fonte autorevole: "
            "se contiene un file, consideralo disponibile anche se una tua "
            "risposta precedente sosteneva che mancasse. Il MANIFEST FILE "
            "INDICIZZATI elenca i file disponibili anche quando nel prompt "
            "sono riportati solo i contenuti pertinenti. Se dopo la patch è "
            "necessario ricostruire OmniProxy, aggiungi anche un solo blocco "
            '<omniproxy-commands>{"commands":["docker compose up -d '
            '--build --force-recreate"]}</omniproxy-commands>. Non proporre '
            "altri comandi shell: il runner accetta soltanto azioni "
            "manutentive predefinite."
        )
    )
    conversation = "\n".join(
        (
            "HANDOFF DALL’ANALISTA"
            if item.get("message_type") == "handoff"
            else str(item["role"]).upper()
        )
        + f": {_builder_display_content(str(item['content']))}"
        for item in history
    )
    return await _build_complete(
        request,
        model_config=model_config,
        system_prompt=(
            f"{role_prompt}\n"
            "Rispondi in italiano. Usa il contesto fornito, segnala quando un "
            "dato non è presente e non esporre eventuali segreti."
        ),
        user_prompt=(
            f"Progetto: {project['name']}\n"
            f"Idea: {project['idea']}\n\n"
            f"Artefatti della pipeline:\n{artifact_context or 'Non ancora generati.'}\n\n"
            f"Snapshot file:\n{project_context or 'Nessun file indicizzato.'}\n\n"
            f"Conversazione:\n{conversation}"
        ),
        output_tokens=(
            8000
            if lane == "builder"
            else (
                900
                if project["analyst_mode"] == "schematic"
                else 2600
            )
        ),
        role_label=(
            "Analista idea" if lane == "analyst" else "Builder"
        ),
    )


async def _validate_gateway_api_config(
    app_instance: FastAPI,
    payload: GatewayApiConfig,
) -> JSONResponse | None:
    catalog = await _model_catalog(app_instance)
    provider = next(
        (item for item in catalog if item["id"] == payload.provider),
        None,
    )
    if provider is None or provider.get("connected") is not True:
        return JSONResponse(
            status_code=409,
            content={
                "error": "provider_not_connected",
                "message": (
                    "Collega o avvia il provider prima di creare questa API."
                ),
            },
            headers={"Cache-Control": "no-store"},
        )
    model = next(
        (
            item
            for item in provider.get("models", [])
            if isinstance(item, dict) and item.get("id") == payload.model
        ),
        None,
    )
    if model is None:
        return JSONResponse(
            status_code=400,
            content={
                "error": "model_not_available",
                "message": "Il modello selezionato non è disponibile.",
            },
            headers={"Cache-Control": "no-store"},
        )
    if payload.reasoning_effort not in model.get("reasoning_efforts", []):
        return JSONResponse(
            status_code=400,
            content={
                "error": "reasoning_not_supported",
                "message": (
                    "Il livello di reasoning non è supportato dal modello."
                ),
            },
            headers={"Cache-Control": "no-store"},
        )
    return None


def _broker_client_for_provider(
    request: Request,
    provider: str,
) -> AntigravityBrokerClient | CodexBrokerClient | ClaudeBrokerClient:
    if provider == "codex":
        return CodexBrokerClient(request.app.state.codex_broker_http)
    if provider == "gemini":
        return AntigravityBrokerClient(
            request.app.state.gemini_broker_http
        )
    if provider == "claude":
        return ClaudeBrokerClient(request.app.state.claude_broker_http)
    raise RuntimeError("Provider broker non supportato.")


def _ollama_think_for_route(route: ModelRoute) -> bool:
    if route.reasoning_effort == "on":
        return True
    if route.reasoning_effort == "off":
        return False
    return settings.ollama_think


async def _broker_available(client: httpx.AsyncClient) -> bool:
    try:
        response = await client.get("/healthz", timeout=2.0)
        return response.is_success
    except httpx.HTTPError:
        return False


async def _discover_ollama(
    app: FastAPI,
    *,
    timeout: float,
) -> tuple[httpx.AsyncClient, dict[str, object]] | None:
    async def probe(
        client: httpx.AsyncClient,
    ) -> tuple[httpx.AsyncClient, dict[str, object]] | None:
        try:
            await enforce_upstream_transport(client.base_url)
            response = await client.get("/api/tags", timeout=timeout)
            payload = response.json()
        except (httpx.HTTPError, ValueError, GatewayError):
            return None
        if not response.is_success or not isinstance(payload, dict):
            return None
        return client, payload

    results = await asyncio.gather(
        *[probe(client) for client in app.state.ollama_http_clients]
    )
    return next((result for result in results if result is not None), None)


async def _require_ollama_client(app: FastAPI) -> httpx.AsyncClient:
    discovered = await _discover_ollama(app, timeout=3.0)
    if discovered is None:
        raise GatewayError(
            503,
            "Nessun container Ollama raggiungibile.",
            error_type="service_unavailable",
            code="ollama_unavailable",
        )
    return discovered[0]


async def _record_usage_safely(
    *,
    request_id: str,
    api_key_id: int,
    route: ModelRoute,
    prompt_tokens: int,
    completion_tokens: int,
    status_code: int,
    started: float,
    error_code: str | None = None,
) -> None:
    try:
        await record_usage(
            request_id=request_id,
            api_key_id=api_key_id,
            requested_model=route.requested_model,
            routed_provider=route.provider,
            resolved_model=route.resolved_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            status_code=status_code,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            error_code=error_code,
        )
    except Exception:
        logger.exception("Could not persist usage for request_id=%s", request_id)
