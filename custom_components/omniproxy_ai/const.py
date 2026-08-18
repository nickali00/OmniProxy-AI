"""Constants for the OmniProxy AI Home Assistant integration."""

DOMAIN = "omniproxy_ai"
PLATFORMS = ["conversation"]

CONF_MODEL = "model"
CONF_SYSTEM_PROMPT = "system_prompt"
CONF_MAX_TOKENS = "max_tokens"
CONF_TEMPERATURE = "temperature"
CONF_MAX_HISTORY = "max_history"
CONF_INCLUDE_EXPOSED_ENTITIES = "include_exposed_entities"

DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
DEFAULT_SYSTEM_PROMPT = (
    "You are a concise Home Assistant voice assistant. Reply in the same "
    "language as the user. You have read-only access to the relevant entity "
    "states supplied in HOME_ASSISTANT_RELEVANT_STATE_CONTEXT. When that "
    "context contains entities, never say that you cannot see Home Assistant. "
    "Use only those states for questions about the home. Home control commands "
    "are handled safely by Home Assistant's local Assist intent engine before "
    "a message reaches you; never claim that you executed an action yourself."
)
LEGACY_SYSTEM_PROMPTS = (
    (
        "You are a concise Home Assistant voice assistant. Reply in the same "
        "language as the user. This connector can answer questions and generate "
        "text, but it cannot directly execute Home Assistant services yet."
    ),
    (
        "You are a concise Home Assistant voice assistant. Reply in the same "
        "language as the user. You may receive a read-only snapshot of entities "
        "that the user explicitly exposed to Assist. Use only that snapshot for "
        "questions about the home. This connector cannot directly execute Home "
        "Assistant services yet."
    ),
    (
        "You are a concise Home Assistant voice assistant. Reply in the same "
        "language as the user. You have read-only access to the relevant entity "
        "states supplied in HOME_ASSISTANT_RELEVANT_STATE_CONTEXT. When that "
        "context contains entities, never say that you cannot see Home Assistant. "
        "Use only those states for questions about the home. Read access is not "
        "control: this connector cannot execute Home Assistant services yet."
    ),
)
DEFAULT_MAX_TOKENS = 512
DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_HISTORY = 6
DEFAULT_INCLUDE_EXPOSED_ENTITIES = True
DEFAULT_REQUEST_TIMEOUT = 120
DEFAULT_VALIDATION_TIMEOUT = 10
MAX_RELEVANT_ENTITY_CONTEXT = 40
