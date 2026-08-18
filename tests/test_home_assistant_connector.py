from __future__ import annotations

import importlib.util
import json
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
    assert manifest["version"] == "0.3.0"


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
