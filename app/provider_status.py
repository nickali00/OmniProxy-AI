from dataclasses import asdict, dataclass

from app.config import settings


@dataclass(frozen=True)
class ProviderStatus:
    id: str
    name: str
    eyebrow: str
    description: str
    state: str
    state_label: str
    auth_method: str
    client_label: str
    action_label: str
    accent: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def dashboard_providers() -> list[dict[str, str]]:
    """
    Metadati iniziali non sensibili della dashboard.

    Lo stato reale viene interrogato via API dai sidecar di OmniProxy. Non
    dipende da applicazioni, estensioni o sessioni installate sull'host.
    """
    return [
        ProviderStatus(
            id="ollama",
            name="Ollama",
            eyebrow="Locale · GPU",
            description=(
                "Inferenza privata sulla RTX 5070 Ti. Se il container è "
                "raggiungibile diventa il motore degli alias base e local."
            ),
            state="checking",
            state_label="Ricerca container",
            auth_method="Nessuna credenziale cloud",
            client_label=settings.ollama_model,
            action_label="Verifica container",
            accent="mint",
        ).as_dict(),
        ProviderStatus(
            id="codex",
            name="Codex",
            eyebrow="OpenAI · ChatGPT",
            description=(
                "Codex CLI e app-server vivono dentro OmniProxy, con sessione "
                "ChatGPT e volume dedicati."
            ),
            state="checking",
            state_label="Verifica broker",
            auth_method="Login ChatGPT",
            client_label=settings.codex_client_label,
            action_label="Collega account",
            accent="sky",
        ).as_dict(),
        ProviderStatus(
            id="gemini",
            name="Gemini",
            eyebrow="Google · Antigravity",
            description=(
                "Gemini tramite Antigravity CLI ufficiale in modalità "
                "headless. Usa la quota del piano Google AI collegato."
            ),
            state="checking",
            state_label="Verifica broker",
            auth_method="Login Google ufficiale",
            client_label=settings.gemini_client_label,
            action_label="Collega account",
            accent="amber",
        ).as_dict(),
        ProviderStatus(
            id="claude",
            name="Claude",
            eyebrow="Anthropic · Pro / Max",
            description=(
                "Accesso tramite Claude Code ufficiale in un container "
                "isolato. Il login avviene soltanto su Claude.ai."
            ),
            state="checking",
            state_label="Verifica broker",
            auth_method="Login Claude.ai",
            client_label=settings.claude_client_label,
            action_label="Collega account",
            accent="coral",
        ).as_dict(),
    ]
