"""Config flow for the OmniProxy AI Home Assistant integration."""

from __future__ import annotations

import hashlib
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TemplateSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    OmniProxyAuthenticationError,
    OmniProxyClient,
    OmniProxyConnectionError,
    OmniProxyResponseError,
    normalize_base_url,
)
from .const import (
    CONF_INCLUDE_EXPOSED_ENTITIES,
    CONF_MAX_HISTORY,
    CONF_MAX_TOKENS,
    CONF_MODEL,
    CONF_SYSTEM_PROMPT,
    CONF_TEMPERATURE,
    DEFAULT_BASE_URL,
    DEFAULT_INCLUDE_EXPOSED_ENTITIES,
    DEFAULT_MAX_HISTORY,
    DEFAULT_MAX_TOKENS,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TEMPERATURE,
    DEFAULT_VALIDATION_TIMEOUT,
    DOMAIN,
)


def _connection_schema(
    suggested: dict[str, Any] | None = None,
) -> vol.Schema:
    values = suggested or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_URL,
                default=values.get(CONF_URL, DEFAULT_BASE_URL),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Required(CONF_API_KEY): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
    )


def _options_schema(options: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_INCLUDE_EXPOSED_ENTITIES,
                default=options.get(
                    CONF_INCLUDE_EXPOSED_ENTITIES,
                    DEFAULT_INCLUDE_EXPOSED_ENTITIES,
                ),
            ): BooleanSelector(),
            vol.Required(
                CONF_SYSTEM_PROMPT,
                default=options.get(CONF_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT),
            ): TemplateSelector(),
            vol.Required(
                CONF_MAX_TOKENS,
                default=options.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=64,
                    max=4096,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_TEMPERATURE,
                default=options.get(CONF_TEMPERATURE, DEFAULT_TEMPERATURE),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=2,
                    step=0.1,
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                CONF_MAX_HISTORY,
                default=options.get(CONF_MAX_HISTORY, DEFAULT_MAX_HISTORY),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=20,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                )
            ),
        }
    )


async def _async_validate(
    hass,
    base_url: str,
    api_key: str,
) -> tuple[str, list[str]]:
    normalized_url = normalize_base_url(base_url)
    client = OmniProxyClient(
        async_get_clientsession(hass),
        normalized_url,
        api_key,
        request_timeout=DEFAULT_REQUEST_TIMEOUT,
    )
    models = await client.async_models(timeout=DEFAULT_VALIDATION_TIMEOUT)
    return normalized_url, models


class OmniProxyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create and validate an OmniProxy AI connection."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial connection form."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            try:
                base_url, models = await _async_validate(
                    self.hass,
                    user_input[CONF_URL],
                    api_key,
                )
            except ValueError:
                errors["base"] = "invalid_url"
            except OmniProxyAuthenticationError:
                errors["base"] = "invalid_auth"
            except OmniProxyConnectionError:
                errors["base"] = "cannot_connect"
            except OmniProxyResponseError:
                errors["base"] = "invalid_response"
            else:
                model = models[0]
                unique_source = f"{base_url}|{model}"
                await self.async_set_unique_id(
                    hashlib.sha256(unique_source.encode()).hexdigest()
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"OmniProxy AI · {model}",
                    data={
                        CONF_URL: base_url,
                        CONF_API_KEY: api_key,
                        CONF_MODEL: model,
                    },
                    options={
                        CONF_INCLUDE_EXPOSED_ENTITIES: (
                            DEFAULT_INCLUDE_EXPOSED_ENTITIES
                        ),
                        CONF_SYSTEM_PROMPT: DEFAULT_SYSTEM_PROMPT,
                        CONF_MAX_TOKENS: DEFAULT_MAX_TOKENS,
                        CONF_TEMPERATURE: DEFAULT_TEMPERATURE,
                        CONF_MAX_HISTORY: DEFAULT_MAX_HISTORY,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(user_input),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Start reauthentication after the gateway rejects a key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Replace a revoked or rotated local API key."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            try:
                base_url, models = await _async_validate(
                    self.hass,
                    entry.data[CONF_URL],
                    api_key,
                )
            except OmniProxyAuthenticationError:
                errors["base"] = "invalid_auth"
            except OmniProxyConnectionError:
                errors["base"] = "cannot_connect"
            except (OmniProxyResponseError, ValueError):
                errors["base"] = "invalid_response"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        **entry.data,
                        CONF_URL: base_url,
                        CONF_API_KEY: api_key,
                        CONF_MODEL: models[0],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OmniProxyOptionsFlow:
        """Return the options editor."""
        return OmniProxyOptionsFlow()


class OmniProxyOptionsFlow(config_entries.OptionsFlow):
    """Edit prompt and response limits without reconnecting."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show and save connector options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(dict(self.config_entry.options)),
        )
