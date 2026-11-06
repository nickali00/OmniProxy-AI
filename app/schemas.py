from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.multimodal import (
    MAX_IMAGES_PER_REQUEST,
    MAX_TOTAL_IMAGE_BYTES,
    ImagePayload,
    parse_image_data_url,
)


class ChatMessage(BaseModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def validate_multimodal_content(self) -> "ChatMessage":
        if isinstance(self.content, list):
            for part in self.content:
                part_type = part.get("type")
                if part_type not in {"text", "input_text", "image_url", "file"}:
                    raise ValueError(
                        "Tipo di contenuto non supportato: sono ammessi "
                        "solo testo, image_url e file caricati."
                    )
                if part_type in {"text", "input_text"} and not isinstance(
                    part.get("text"), str
                ):
                    raise ValueError("Una parte di testo richiede text stringa.")
                if part_type == "file":
                    attachment = part.get("file")
                    if (self.role != "user" or not isinstance(attachment, dict)
                        or set(attachment) - {"file_id", "mode", "image_only"}
                        or not isinstance(attachment.get("file_id"), str)
                        or not 1 <= len(attachment["file_id"]) <= 100
                        or attachment.get("mode", "auto") not in {"auto", "text", "vision"}
                        or not isinstance(attachment.get("image_only", False), bool)):
                        raise ValueError("Riferimento file non valido.")
        images = self.image_payloads()
        if images and self.role != "user":
            raise ValueError(
                "Le immagini image_url sono ammesse solo nei messaggi user."
            )
        return self

    def text_content(self) -> str:
        if isinstance(self.content, str):
            return self.content
        if not isinstance(self.content, list):
            return ""

        text_parts: list[str] = []
        for part in self.content:
            if part.get("type") in {"text", "input_text"}:
                text = part.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
        return "\n".join(text_parts)

    def image_payloads(self) -> list[ImagePayload]:
        if not isinstance(self.content, list):
            return []
        images: list[ImagePayload] = []
        for part in self.content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            image_url = part.get("image_url")
            if not isinstance(image_url, dict):
                raise ValueError(
                    "image_url deve essere un oggetto con il campo url."
                )
            images.append(parse_image_data_url(image_url.get("url")))
        return images

    def provider_content(self) -> str | list[dict[str, Any]] | None:
        """Return the OpenAI-compatible content after schema validation."""
        return self.content


class StreamOptions(BaseModel):
    include_usage: bool = False

    model_config = ConfigDict(extra="allow")


class ChatCompletionRequest(BaseModel):
    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: StreamOptions | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    stop: str | list[str] | None = None
    seed: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    response_format: dict[str, Any] | None = None
    modalities: list[str] | None = None
    audio: dict[str, Any] | None = None

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def validate_image_limits(self) -> "ChatCompletionRequest":
        if self.response_format is not None:
            raise ValueError("response_format non è ancora supportato.")
        if self.modalities is not None and self.modalities != ["text"]:
            raise ValueError("Sono supportate solo risposte testuali.")
        if self.audio is not None:
            raise ValueError("L'output audio non è ancora supportato.")
        if self.tools is not None:
            names: set[str] = set()
            for tool in self.tools:
                function = tool.get("function") if isinstance(tool, dict) else None
                name = function.get("name") if isinstance(function, dict) else None
                if (
                    tool.get("type") != "function"
                    or not isinstance(name, str)
                    or not name
                    or name in names
                    or (function.get("parameters") is not None and not isinstance(function.get("parameters"), dict))
                ):
                    raise ValueError("Definizione tool non valida o duplicata.")
                names.add(name)
        if isinstance(self.tool_choice, str) and self.tool_choice not in {"auto", "none", "required"}:
            raise ValueError("tool_choice non valido.")
        if isinstance(self.tool_choice, dict):
            function = self.tool_choice.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if self.tool_choice.get("type") != "function" or name not in {
                tool["function"]["name"] for tool in (self.tools or [])
            }:
                raise ValueError("tool_choice indica un tool non disponibile.")
        if any(message.tool_calls or message.role == "tool" for message in self.messages):
            for message in self.messages:
                if message.role == "tool" and not message.tool_call_id:
                    raise ValueError("Un risultato tool richiede tool_call_id.")
                if message.tool_calls and message.role != "assistant":
                    raise ValueError("tool_calls è ammesso solo per assistant.")
        images = [
            image
            for message in self.messages
            for image in message.image_payloads()
        ]
        total_bytes = sum(len(image.data) for image in images)
        if len(images) > MAX_IMAGES_PER_REQUEST:
            raise ValueError(
                f"Sono ammesse al massimo {MAX_IMAGES_PER_REQUEST} immagini "
                "per richiesta."
            )
        if total_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise ValueError(
                "La dimensione totale delle immagini supera il limite di "
                f"{MAX_TOTAL_IMAGE_BYTES // (1024 * 1024)} MiB."
            )
        return self

    @property
    def has_images(self) -> bool:
        return any(message.image_payloads() for message in self.messages)

    @property
    def has_client_tools(self) -> bool:
        return bool(self.tools or self.tool_choice or any(
            message.tool_calls or message.role == "tool" for message in self.messages
        ) or any(
            message.role in {"system", "developer"}
            and "tool_definition" in message.text_content()
            and "TOOL_NAME:" in message.text_content()
            for message in self.messages
        ))

    @property
    def output_token_limit(self) -> int | None:
        return self.max_completion_tokens or self.max_tokens


class ProviderAuthCode(BaseModel):
    code: str = Field(min_length=8, max_length=8192)

    model_config = ConfigDict(extra="forbid")


class GeminiOAuthConfiguration(BaseModel):
    project_id: str = Field(
        min_length=6,
        max_length=128,
        pattern=r"^[a-z][a-z0-9-]{4,126}[a-z0-9]$",
    )
    client_id: str = Field(
        min_length=20,
        max_length=512,
        pattern=r"^[A-Za-z0-9._:-]+\.apps\.googleusercontent\.com$",
    )
    client_secret: str = Field(min_length=8, max_length=512)

    model_config = ConfigDict(extra="forbid")

    @field_validator("project_id", "client_id", "client_secret")
    @classmethod
    def strip_oauth_value(cls, value: str) -> str:
        return value.strip()


class McpToolSelection(BaseModel):
    server_id: int = Field(gt=0)
    tool_names: list[str] = Field(default_factory=list, max_length=100)

    model_config = ConfigDict(extra="forbid")

    @field_validator("tool_names")
    @classmethod
    def normalize_tool_names(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values))
        if any(
            not value
            or len(value) > 128
            or not all(character.isalnum() or character in "._:-" for character in value)
            for value in normalized
        ):
            raise ValueError("Nome tool MCP non valido.")
        return normalized


class SkillConfig(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=500)
    instructions: str = Field(min_length=1, max_length=50_000)
    enabled: bool = True

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def normalize_skill_name(cls, value: str) -> str:
        return " ".join(value.strip().split())

    @field_validator("description", "instructions")
    @classmethod
    def strip_skill_text(cls, value: str) -> str:
        return value.strip()


class McpServerConfig(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    url: str = Field(min_length=8, max_length=2048)
    auth_type: Literal["none", "bearer"] = "none"
    bearer_token: str | None = Field(
        default=None,
        max_length=8192,
        repr=False,
    )
    enabled: bool = True

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def normalize_mcp_name(cls, value: str) -> str:
        return " ".join(value.strip().split())

    @field_validator("url")
    @classmethod
    def strip_mcp_url(cls, value: str) -> str:
        return value.strip()

    @field_validator("bearer_token")
    @classmethod
    def strip_bearer_token(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def validate_auth(self) -> "McpServerConfig":
        if self.auth_type == "none" and self.bearer_token is not None:
            raise ValueError("Un server senza autenticazione non accetta token.")
        return self


class ApiLimits(BaseModel):
    input_tokens: int | None = Field(default=None, ge=1, le=2_000_000)
    context_tokens: int | None = Field(default=None, ge=512, le=2_000_000)
    output_tokens: int | None = Field(default=None, ge=1, le=128_000)
    context_mode: Literal["reject", "summarize"] = "reject"
    token_budget: int | None = Field(default=None, ge=1, le=1_000_000_000_000)
    budget_period: Literal["unlimited", "daily", "weekly", "monthly"] = "unlimited"
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_limits(self) -> "ApiLimits":
        if (self.budget_period == "unlimited") != (self.token_budget is None):
            raise ValueError("Un budget richiede periodo e numero di token; senza limiti non richiede un budget.")
        if self.context_mode == "summarize" and self.context_tokens is None and self.input_tokens is None:
            raise ValueError("La compattazione richiede un limite di contesto o di ingresso.")
        if self.context_tokens and self.output_tokens and self.output_tokens >= self.context_tokens:
            raise ValueError("L'output massimo deve lasciare spazio all'input nel contesto.")
        return self


class GatewayApiConfig(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    provider: Literal["ollama", "codex", "gemini", "claude", "openai_compatible"]
    model: str = Field(min_length=1, max_length=128)
    reasoning_effort: str = Field(min_length=1, max_length=32)
    skill_ids: list[int] = Field(default_factory=list, max_length=50)
    mcp_tools: list[McpToolSelection] = Field(default_factory=list, max_length=20)
    limits: ApiLimits | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return " ".join(value.strip().split())

    @field_validator("model", "reasoning_effort")
    @classmethod
    def strip_value(cls, value: str) -> str:
        return value.strip()

    @field_validator("skill_ids")
    @classmethod
    def normalize_skill_ids(cls, values: list[int]) -> list[int]:
        if any(value <= 0 for value in values):
            raise ValueError("Identificatore Skill non valido.")
        return list(dict.fromkeys(values))

    @property
    def flattened_mcp_tools(self) -> list[tuple[int, str]]:
        return [
            (selection.server_id, tool_name)
            for selection in self.mcp_tools
            for tool_name in selection.tool_names
        ]


class BuildModelConfig(BaseModel):
    provider: Literal["ollama", "codex", "gemini", "claude"]
    model: str = Field(min_length=1, max_length=128)
    reasoning_effort: str = Field(min_length=1, max_length=32)

    model_config = ConfigDict(extra="forbid")

    @field_validator("model", "reasoning_effort")
    @classmethod
    def normalize_value(cls, value: str) -> str:
        return value.strip()


class BuildFileSnapshot(BaseModel):
    path: str = Field(min_length=1, max_length=320)
    content: str = Field(max_length=1_048_576)

    model_config = ConfigDict(extra="forbid")

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        clean_path = value.strip().replace("\\", "/")
        parts = clean_path.split("/")
        if (
            clean_path.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("Il percorso del file deve essere relativo.")
        return clean_path


class BuildProjectConfig(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    folder_name: str = Field(default="", max_length=128)
    idea: str = Field(default="", max_length=12_000)
    analyst_mode: Literal["schematic", "detailed"] = "detailed"
    analyst: BuildModelConfig
    builder: BuildModelConfig
    files: list[BuildFileSnapshot] = Field(
        default_factory=list,
        max_length=2_000,
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return " ".join(value.strip().split())

    @field_validator("folder_name")
    @classmethod
    def normalize_folder_name(cls, value: str) -> str:
        return value.strip()


class BuildFileSync(BaseModel):
    folder_name: str = Field(min_length=1, max_length=128)
    files: list[BuildFileSnapshot] = Field(max_length=2_000)

    model_config = ConfigDict(extra="forbid")

    @field_validator("folder_name")
    @classmethod
    def normalize_folder_name(cls, value: str) -> str:
        return value.strip()


class BuildPlanRequest(BaseModel):
    idea: str = Field(min_length=10, max_length=12_000)

    model_config = ConfigDict(extra="forbid")

    @field_validator("idea")
    @classmethod
    def normalize_idea(cls, value: str) -> str:
        return value.strip()


class BuildChatRequest(BaseModel):
    lane: Literal["analyst", "builder"] = "analyst"
    message: str = Field(min_length=1, max_length=8_000)

    model_config = ConfigDict(extra="forbid")

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        return value.strip()


class BuildHandoffRequest(BaseModel):
    instruction: str = Field(default="", max_length=8_000)

    model_config = ConfigDict(extra="forbid")

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: str) -> str:
        return value.strip()
