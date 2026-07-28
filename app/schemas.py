from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatMessage(BaseModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None

    model_config = ConfigDict(extra="allow")

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

    model_config = ConfigDict(extra="allow")

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


class GatewayApiConfig(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    provider: Literal["ollama", "codex", "gemini", "claude"]
    model: str = Field(min_length=1, max_length=128)
    reasoning_effort: str = Field(min_length=1, max_length=32)

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return " ".join(value.strip().split())

    @field_validator("model", "reasoning_effort")
    @classmethod
    def strip_value(cls, value: str) -> str:
        return value.strip()


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
