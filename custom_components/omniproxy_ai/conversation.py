"""Home Assistant Assist conversation agent backed by OmniProxy AI."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Literal

from homeassistant.components import conversation
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OmniProxyConfigEntry
from .api import (
    OmniProxyApiError,
    OmniProxyAuthenticationError,
    OmniProxyConnectionError,
)
from .const import (
    CONF_INCLUDE_EXPOSED_ENTITIES,
    CONF_MAX_HISTORY,
    CONF_MAX_TOKENS,
    CONF_SYSTEM_PROMPT,
    CONF_TEMPERATURE,
    DEFAULT_INCLUDE_EXPOSED_ENTITIES,
    DEFAULT_MAX_HISTORY,
    DEFAULT_MAX_TOKENS,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TEMPERATURE,
)
from .local_intents import local_intent_candidates, looks_like_control_command
from .state_context import build_exposed_state_context

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: OmniProxyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the OmniProxy AI conversation agent."""
    async_add_entities([OmniProxyConversationEntity(config_entry)])


class OmniProxyConversationEntity(
    conversation.ConversationEntity,
    conversation.AbstractConversationAgent,
):
    """Represent an OmniProxy AI agent in Home Assistant Assist."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:transit-connection-variant"
    _attr_supported_features = conversation.ConversationEntityFeature.CONTROL

    def __init__(self, entry: OmniProxyConfigEntry) -> None:
        self.entry = entry
        self._attr_name = entry.title
        self._attr_unique_id = entry.entry_id

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """OmniProxy providers can answer in any supported model language."""
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        """Register the entity as a selectable conversation agent."""
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister the conversation agent."""
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Handle safe local intents, then fall back to the configured LLM."""
        for candidate in local_intent_candidates(user_input.text):
            candidate_input = (
                user_input
                if candidate == user_input.text
                else replace(user_input, text=candidate)
            )
            local_response = await conversation.async_handle_intents(
                self.hass,
                candidate_input,
                chat_log,
            )
            if local_response is not None:
                speech = local_response.speech.get("plain", {}).get("speech", "")
                chat_log.async_add_assistant_content_without_tools(
                    conversation.AssistantContent(
                        agent_id=user_input.agent_id,
                        content=speech,
                    )
                )
                return conversation.ConversationResult(
                    response=local_response,
                    conversation_id=chat_log.conversation_id,
                )

        options = self.entry.options
        system_prompt = str(
            options.get(CONF_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT)
        ).strip()
        if user_input.extra_system_prompt:
            system_prompt = (
                f"{system_prompt}\n\n{user_input.extra_system_prompt.strip()}"
            )
        if looks_like_control_command(user_input.text):
            system_prompt = (
                f"{system_prompt}\n\nThis message looks like a home-control "
                "request, but Home Assistant's local intent engine did not "
                "match it. Do not say that the connector lacks control. "
                "Explain concisely that the target may not be exposed to "
                "Assist or its exact name/alias did not match. Do not claim "
                "that the action was executed."
            )
        if bool(
            options.get(
                CONF_INCLUDE_EXPOSED_ENTITIES,
                DEFAULT_INCLUDE_EXPOSED_ENTITIES,
            )
        ):
            system_prompt = (
                f"{system_prompt}\n\n"
                f"{build_exposed_state_context(self.hass, user_input.text)}"
            )

        max_history = int(options.get(CONF_MAX_HISTORY, DEFAULT_MAX_HISTORY))
        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        conversational_content = [
            item
            for item in chat_log.content
            if item.role in {"user", "assistant"}
            and isinstance(getattr(item, "content", None), str)
            and item.content.strip()
        ]
        if max_history > 0:
            conversational_content = conversational_content[-(max_history * 2 + 1) :]
        else:
            conversational_content = conversational_content[-1:]
        messages.extend(
            {"role": item.role, "content": item.content}
            for item in conversational_content
        )

        try:
            answer = await self.entry.runtime_data.client.async_chat(
                model=self.entry.runtime_data.model,
                messages=messages,
                max_tokens=int(options.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS)),
                temperature=float(options.get(CONF_TEMPERATURE, DEFAULT_TEMPERATURE)),
            )
        except OmniProxyAuthenticationError:
            _LOGGER.warning("OmniProxy AI rejected the configured local API key")
            answer = self._error_message(user_input.language, "auth")
        except OmniProxyConnectionError:
            _LOGGER.warning("Home Assistant could not reach OmniProxy AI")
            answer = self._error_message(user_input.language, "connection")
        except OmniProxyApiError:
            _LOGGER.exception("OmniProxy AI returned an invalid response")
            answer = self._error_message(user_input.language, "response")

        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(
                agent_id=user_input.agent_id,
                content=answer,
            )
        )
        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    @staticmethod
    def _error_message(language: str, error: str) -> str:
        """Return short spoken errors in the two primary project languages."""
        italian = language.lower().startswith("it")
        if error == "auth":
            return (
                "La chiave locale di OmniProxy AI non è valida o è stata sospesa."
                if italian
                else "The OmniProxy AI local key is invalid or paused."
            )
        if error == "connection":
            return (
                "Non riesco a raggiungere OmniProxy AI. Controlla indirizzo e porta."
                if italian
                else "I cannot reach OmniProxy AI. Check its address and port."
            )
        return (
            "OmniProxy AI ha restituito una risposta non valida."
            if italian
            else "OmniProxy AI returned an invalid response."
        )
