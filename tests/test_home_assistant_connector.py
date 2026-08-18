from __future__ import annotations

import importlib.util
import json
import struct
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "omniproxy_ai"


def _load_api_module():
    """Load the standalone URL helpers without requiring Home Assistant."""
    sys.modules.setdefault(
        "aiohttp",
        SimpleNamespace(
            ClientSession=object,
            ClientError=Exception,
            ContentTypeError=ValueError,
        ),
    )
    spec = importlib.util.spec_from_file_location(
        "omniproxy_home_assistant_api",
        COMPONENT / "api.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_local_intents_module():
    """Load local command normalization without requiring Home Assistant."""
    spec = importlib.util.spec_from_file_location(
        "omniproxy_home_assistant_local_intents",
        COMPONENT / "local_intents.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_state_context_module():
    """Load state relevance helpers without installing Home Assistant."""
    package_name = "omniproxy_home_assistant_test"
    package = ModuleType(package_name)
    package.__path__ = [str(COMPONENT)]
    sys.modules[package_name] = package

    for module_name in ("const", "state_context"):
        qualified_name = f"{package_name}.{module_name}"
        spec = importlib.util.spec_from_file_location(
            qualified_name,
            COMPONENT / f"{module_name}.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified_name] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{package_name}.state_context"]


def test_manifest_is_a_hacs_installable_config_flow():
    manifest = json.loads((COMPONENT / "manifest.json").read_text())

    assert manifest["domain"] == "omniproxy_ai"
    assert manifest["config_flow"] is True
    assert manifest["dependencies"] == ["conversation"]
    assert manifest["version"] == "0.4.1"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://127.0.0.1:8181", "http://127.0.0.1:8181/v1"),
        ("http://gateway:8000/v1/", "http://gateway:8000/v1"),
        (
            "https://omniproxy.example.test/private",
            "https://omniproxy.example.test/private/v1",
        ),
    ],
)
def test_normalize_base_url(value, expected):
    api = _load_api_module()
    assert api.normalize_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ftp://gateway:8000",
        "http://user:secret@gateway:8000",
        "http://gateway:invalid",
        "http://gateway:8000?v=1",
    ],
)
def test_normalize_base_url_rejects_unsafe_or_invalid_values(value):
    api = _load_api_module()
    with pytest.raises(ValueError):
        api.normalize_base_url(value)


def test_parse_model_ids_uses_gateway_managed_slug():
    api = _load_api_module()
    payload = {
        "object": "list",
        "data": [
            {
                "id": "home-assistant-gemini",
                "object": "model",
                "owned_by": "omni-proxy-gemini",
            }
        ],
    }

    assert api.parse_model_ids(payload) == ["home-assistant-gemini"]


def test_all_connector_translations_are_valid_json():
    base = json.loads((COMPONENT / "strings.json").read_text())
    assert base["config"]["step"]["user"]["data"]["api_key"]

    for language in ("en", "it", "es", "fr"):
        translation = json.loads(
            (COMPONENT / "translations" / f"{language}.json").read_text()
        )
        assert translation["title"] == "OmniProxy AI"
        assert translation["config"]["error"]["cannot_connect"]


def test_conversation_agent_routes_controls_through_local_assist_first():
    source = (COMPONENT / "conversation.py").read_text()

    assert "ConversationEntityFeature.CONTROL" in source
    assert "await conversation.async_handle_intents(" in source
    assert source.index("await conversation.async_handle_intents(") < source.index(
        "async_chat("
    )


@pytest.mark.parametrize(
    ("phrase", "normalized"),
    [
        (
            "Puoi accendere il condizionatore di Nicola?",
            "accendi il condizionatore di Nicola?",
        ),
        ("Mi puoi spegnere la luce?", "spegni la luce?"),
        ("Per favore, potresti impostare 23 gradi?", "imposta 23 gradi?"),
    ],
)
def test_polite_italian_controls_get_a_local_assist_candidate(phrase, normalized):
    local_intents = _load_local_intents_module()

    assert local_intents.local_intent_candidates(phrase) == (phrase, normalized)
    assert local_intents.looks_like_control_command(phrase)


def test_connector_migrates_only_known_legacy_default_prompts():
    init_source = (COMPONENT / "__init__.py").read_text()
    config_flow_source = (COMPONENT / "config_flow.py").read_text()

    assert "options.get(CONF_SYSTEM_PROMPT) in LEGACY_SYSTEM_PROMPTS" in init_source
    assert "VERSION = 2" in config_flow_source


@pytest.mark.parametrize(
    ("filename", "expected_size"),
    [("icon.png", 256), ("icon@2x.png", 512)],
)
def test_local_brand_icons_are_square_rgba_png(filename, expected_size):
    image = (COMPONENT / "brand" / filename).read_bytes()

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", image[16:24])
    assert (width, height) == (expected_size, expected_size)
    assert image[25] == 6  # PNG color type 6: RGBA


def test_state_context_sends_only_relevant_exposed_entities():
    state_context = _load_state_context_module()
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    states = [
        SimpleNamespace(
            entity_id="sensor.phone_battery",
            name="Phone battery",
            state="42",
            attributes={
                "device_class": "battery",
                "unit_of_measurement": "%",
                "private_token": "must-not-leave-home-assistant",
            },
            last_changed=now,
        ),
        SimpleNamespace(
            entity_id="sensor.kitchen_temperature",
            name="Kitchen temperature",
            state="21.5",
            attributes={
                "device_class": "temperature",
                "unit_of_measurement": "°C",
            },
            last_changed=now,
        ),
        SimpleNamespace(
            entity_id="sensor.secret_battery",
            name="Unexposed battery",
            state="10",
            attributes={"device_class": "battery"},
            last_changed=now,
        ),
    ]
    hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda: states))

    rendered = state_context.build_exposed_state_context(
        hass,
        "Quali sono le batterie di casa?",
        should_expose=lambda entity_id: entity_id != "sensor.secret_battery",
    )
    payload = json.loads(rendered.rsplit("\n", maxsplit=1)[-1])

    assert payload["total_exposed"] == 2
    assert [item["entity_id"] for item in payload["entities"]] == [
        "sensor.phone_battery"
    ]
    assert payload["entities"][0]["state"] == "42"
    assert "private_token" not in payload["entities"][0]


def test_state_context_understands_sensor_catalog_questions():
    state_context = _load_state_context_module()
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    states = [
        SimpleNamespace(
            entity_id="sensor.phone_battery",
            name="Batteria telefono",
            state="42",
            attributes={"device_class": "battery"},
            last_changed=now,
        ),
        SimpleNamespace(
            entity_id="binary_sensor.front_door",
            name="Porta ingresso",
            state="off",
            attributes={"device_class": "door"},
            last_changed=now,
        ),
        SimpleNamespace(
            entity_id="light.kitchen",
            name="Luce cucina",
            state="on",
            attributes={},
            last_changed=now,
        ),
    ]
    hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda: states))

    rendered = state_context.build_exposed_state_context(
        hass,
        "Quali sensori riesci a vedere?",
        should_expose=lambda _entity_id: True,
    )
    payload = json.loads(rendered.rsplit("\n", maxsplit=1)[-1])

    assert payload["selection_mode"] == "catalog"
    assert [item["entity_id"] for item in payload["entities"]] == [
        "binary_sensor.front_door",
        "sensor.phone_battery",
    ]
    assert "DO have read-only visibility" in rendered


def test_state_context_can_list_all_visible_entities_on_explicit_request():
    state_context = _load_state_context_module()
    state = SimpleNamespace(
        entity_id="light.kitchen",
        name="Luce cucina",
        state="on",
        attributes={},
        last_changed=None,
    )
    hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda: [state]))

    rendered = state_context.build_exposed_state_context(
        hass,
        "Non puoi vedere tu le entità?",
        should_expose=lambda _entity_id: True,
    )
    payload = json.loads(rendered.rsplit("\n", maxsplit=1)[-1])

    assert payload["selection_mode"] == "catalog"
    assert payload["entities"][0]["entity_id"] == "light.kitchen"
