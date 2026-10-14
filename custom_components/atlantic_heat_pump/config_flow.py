"""Configuration flow for the Atlantic heat-pump controller."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_DEADBAND,
    CONF_DELTA_MAX,
    CONF_IDLE_OFFSET,
    CONF_REFERENCE_SENSOR,
    CONF_ROOM_CLIMATES,
    CONF_TARGET_CLIMATE,
    DEFAULT_DEADBAND,
    DEFAULT_DELTA_MAX,
    DEFAULT_IDLE_OFFSET,
    DOMAIN,
)


def _data_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}

    def required_entity(key: str):
        if key in defaults:
            return vol.Required(key, default=defaults[key])
        return vol.Required(key)

    return vol.Schema(
        {
            required_entity(CONF_TARGET_CLIMATE): EntitySelector(
                EntitySelectorConfig(domain="climate")
            ),
            required_entity(CONF_REFERENCE_SENSOR): EntitySelector(
                EntitySelectorConfig(domain="sensor")
            ),
            vol.Required(
                CONF_ROOM_CLIMATES,
                default=defaults.get(CONF_ROOM_CLIMATES, []),
            ): EntitySelector(
                EntitySelectorConfig(domain="climate", multiple=True)
            ),
            vol.Required(
                CONF_DEADBAND,
                default=defaults.get(CONF_DEADBAND, DEFAULT_DEADBAND),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0.05,
                    max=5,
                    step=0.05,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_DELTA_MAX,
                default=defaults.get(CONF_DELTA_MAX, DEFAULT_DELTA_MAX),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0.5,
                    max=15,
                    step=0.5,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_IDLE_OFFSET,
                default=defaults.get(CONF_IDLE_OFFSET, DEFAULT_IDLE_OFFSET),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=-3,
                    max=3,
                    step=0.1,
                    mode=NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _validate_input(user_input: dict[str, Any]) -> str | None:
    if not user_input[CONF_ROOM_CLIMATES]:
        return "no_rooms"
    if user_input[CONF_TARGET_CLIMATE] in user_input[CONF_ROOM_CLIMATES]:
        return "target_is_room"
    return None


class AtlanticHeatPumpConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up an Atlantic heat-pump controller."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            error = _validate_input(user_input)
            if error is not None:
                errors["base"] = error
            else:
                await self.async_set_unique_id(user_input[CONF_TARGET_CLIMATE])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_TARGET_CLIMATE],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_data_schema(user_input),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AtlanticHeatPumpOptionsFlow:
        """Return the options flow."""
        return AtlanticHeatPumpOptionsFlow(config_entry)


class AtlanticHeatPumpOptionsFlow(config_entries.OptionsFlow):
    """Allow changing entity selections and modulation values."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        defaults = {**self.config_entry.data, **self.config_entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            error = _validate_input(user_input)
            if error is not None:
                errors["base"] = error
            else:
                return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=_data_schema(defaults),
            errors=errors,
        )
