from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "OmniProxy AI Gateway"
    app_version: str = "0.1.0"
    log_level: str = "INFO"
    build_enabled: bool = False

    database_path: Path = Path("/data/omni_proxy.sqlite3")
    bootstrap_api_key: str = ""

    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "qwen3:8b"
    ollama_keep_alive: str = "10m"
    ollama_think: bool = False
    ollama_request_timeout_seconds: float = Field(default=300.0, gt=0)

    external_mock_latency_seconds: float = Field(default=0.05, ge=0)
    tiktoken_encoding: str = "cl100k_base"

    # I provider cloud usano esclusivamente i rispettivi client ufficiali in
    # sidecar autonomi. Nessuna sessione viene letta da VS Code o dalla home
    # dell'host.
    codex_client_label: str = "Codex CLI 0.145.0"
    gemini_client_label: str = "Antigravity CLI 1.1.7"
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
