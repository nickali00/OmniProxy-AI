from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "OmniProxy AI Gateway"
    app_version: str = "1.0.0-rc.4"
    log_level: str = "INFO"
    build_enabled: bool = False
    job_workspace_roots: str = ""
    jobs_max_active_per_key: int = Field(default=4, ge=1, le=32)
    jobs_lease_seconds: int = Field(default=60, ge=10, le=600)
    jobs_retention_days: int = Field(default=0, ge=0, le=3650)
    job_test_lease_seconds: int = Field(default=60, ge=30, le=600)
    job_test_timeout_seconds: int = Field(default=30, ge=1, le=300)
    token_budget_timezone: str = "Europe/Rome"
    default_budget_output_tokens: int = Field(default=4096, ge=1, le=128_000)
    max_request_body_bytes: int = Field(default=32 * 1024 * 1024, ge=1024, le=128 * 1024 * 1024)
    request_body_timeout_seconds: float = Field(default=30, ge=1, le=300)
    max_concurrent_api_requests: int = Field(default=16, ge=1, le=256)
    file_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    file_storage_bytes_per_key: int = Field(default=100 * 1024 * 1024, ge=1024)
    file_retention_days: int = Field(default=7, ge=1, le=365)
    file_processing_timeout_seconds: float = Field(default=60, ge=1, le=300)
    file_ocr_languages: str = "eng"
    file_max_video_seconds: float = Field(default=120, ge=1, le=3600)
    file_video_frames: int = Field(default=4, ge=1, le=8)
    file_max_audio_seconds: float = Field(default=300, ge=1, le=3600)
    audio_backend_url: str = ""
    audio_api_key: str = ""
    audio_model: str = "whisper-1"
    audio_local_model: str = ""
    audio_timeout_seconds: float = Field(default=120, ge=1, le=900)
    audio_input_tokens_per_second: float = Field(default=50, gt=0, le=1000)
    speech_timeout_seconds: float = Field(default=45, ge=1, le=300)
    speech_max_seconds: float = Field(default=120, ge=1, le=300)
    speech_output_tokens_per_second: float = Field(default=50, gt=0, le=1000)
    image_backend_url: str = ""
    image_api_key: str = ""
    image_model: str = ""
    image_local_model: str = ""
    image_local_steps: int = Field(default=20, ge=1, le=50)
    image_timeout_seconds: float = Field(default=120, ge=1, le=300)
    image_max_pixels: int = Field(default=1572864, ge=65536, le=4194304)
    image_output_tokens_per_megapixel: int = Field(default=4096, ge=1, le=128000)

    database_path: Path = Path("/data/omni_proxy.sqlite3")
    database_url: str = Field(default="", repr=False)
    bootstrap_api_key: str = ""
    admin_auth_required: bool = False
    dashboard_auth_required: bool = False
    require_https: bool = False
    maintenance_token_path: Path | None = None
    auth_failures_per_minute: int = Field(default=30, ge=5, le=1000)
    otel_enabled: bool = False
    otel_endpoint: str = ""
    otel_sample_rate: float = Field(default=0.1, ge=0, le=1)

    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "qwen3:8b"
    ollama_keep_alive: str = "10m"
    ollama_think: bool = False
    ollama_context_tokens: int = Field(default=4096, ge=512, le=262144)
    ollama_request_timeout_seconds: float = Field(default=300.0, gt=0)

    upstream_base_url: str = ""
    upstream_api_key: str = ""
    upstream_model: str = ""
    upstream_deployments_json: str = Field(default="[]", repr=False)

    external_mock_latency_seconds: float = Field(default=0.05, ge=0)
    tiktoken_encoding: str = "cl100k_base"

    # I provider cloud usano esclusivamente i rispettivi client ufficiali in
    # sidecar autonomi. Nessuna sessione viene letta da VS Code o dalla home
    # dell'host.
    codex_client_label: str = "Codex CLI 0.156.1"
    gemini_client_label: str = "Antigravity CLI"
    claude_client_label: str = "Claude Code 2.1.218"
    codex_broker_url: str = "http://codex-broker:8788"
    gemini_broker_url: str = "http://antigravity-broker:8789"
    claude_broker_url: str = "http://claude-broker:8787"
    provider_broker_timeout_seconds: float = Field(default=25.0, gt=0, le=60)
    provider_completion_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        le=900,
    )
    maintenance_runner_url: str = "http://maintenance-runner:8790"
    maintenance_workspace_name: str = "OmniProxy AI"

    # Le integrazioni MCP sono sempre amministrate dal gateway. I timeout e i
    # limiti impediscono a un server lento o a un tool troppo verboso di
    # monopolizzare una richiesta OpenAI-compatible.
    mcp_connect_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    mcp_call_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    mcp_max_tool_calls: int = Field(default=4, ge=1, le=10)
    mcp_max_result_characters: int = Field(default=48_000, ge=1_000, le=200_000)

    # Parametri legacy del connettore Gemini Developer API. Restano disponibili
    # soltanto al modulo separato e non sono usati dal routing Antigravity.
    provider_vault_key: str = ""
    provider_vault_key_path: Path | None = None
    gemini_oauth_authorize_url: str = (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )
    gemini_oauth_token_url: str = "https://oauth2.googleapis.com/token"
    gemini_oauth_revoke_url: str = "https://oauth2.googleapis.com/revoke"
    gemini_api_base_url: str = "https://generativelanguage.googleapis.com"
    gemini_oauth_redirect_uri: str = ""
    gemini_oauth_ttl_seconds: int = Field(default=600, ge=120, le=1800)

    # L'interfaccia amministrativa è intenzionalmente raggiungibile solo da
    # nomi host loopback. `testserver` serve esclusivamente ai test FastAPI.
    dashboard_allowed_hosts: str = "127.0.0.1,localhost,gateway,testserver"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
