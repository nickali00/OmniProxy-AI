"""OmniProxy AI integration for Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    OmniProxyAuthenticationError,
    OmniProxyClient,
    OmniProxyConnectionError,
    OmniProxyResponseError,
)
from .const import CONF_MODEL, DEFAULT_REQUEST_TIMEOUT


@dataclass(slots=True)
class OmniProxyRuntimeData:
    """Runtime data shared by OmniProxy AI platforms."""

    client: OmniProxyClient
    model: str


OmniProxyConfigEntry = ConfigEntry[OmniProxyRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: OmniProxyConfigEntry) -> bool:
    """Set up OmniProxy AI from a UI-created config entry."""
    client = OmniProxyClient(
        async_get_clientsession(hass),
        entry.data[CONF_URL],
        entry.data[CONF_API_KEY],
        request_timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    try:
        models = await client.async_models()
    except OmniProxyAuthenticationError as exc:
        raise ConfigEntryAuthFailed from exc
    except OmniProxyConnectionError as exc:
        raise ConfigEntryNotReady(str(exc)) from exc
    except OmniProxyResponseError as exc:
        raise ConfigEntryNotReady(str(exc)) from exc

    model = entry.data[CONF_MODEL]
    if model not in models:
        raise ConfigEntryNotReady(
            f"Configured OmniProxy AI model '{model}' is no longer available"
        )

    entry.runtime_data = OmniProxyRuntimeData(client=client, model=model)
    await hass.config_entries.async_forward_entry_setups(
        entry, (Platform.CONVERSATION,)
    )
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OmniProxyConfigEntry) -> bool:
    """Unload the OmniProxy AI integration."""
    return await hass.config_entries.async_unload_platforms(
        entry, (Platform.CONVERSATION,)
    )


async def async_reload_entry(hass: HomeAssistant, entry: OmniProxyConfigEntry) -> None:
    """Reload after the integration options change."""
    await hass.config_entries.async_reload(entry.entry_id)
