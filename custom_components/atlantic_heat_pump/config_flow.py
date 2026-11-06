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
    CONF_ROOM_CLIMATES,
    CONF_ROOM_WEIGHT,
    CONF_ROOM_WEIGHTS,
    CONF_TARGET_CLIMATE,
    DEFAULT_DEADBAND,
    DEFAULT_DELTA_MAX,
    DEFAULT_ROOM_WEIGHT,
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
        }
    )


def _validate_input(user_input: dict[str, Any]) -> str | None:
    if not user_input[CONF_ROOM_CLIMATES]:
        return "no_rooms"
    if user_input[CONF_TARGET_CLIMATE] in user_input[CONF_ROOM_CLIMATES]:
        return "target_is_room"
    return None


def _room_weight_schema(default: float = DEFAULT_ROOM_WEIGHT) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_ROOM_WEIGHT, default=default): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=100,
                    step=5,
                    mode=NumberSelectorMode.SLIDER,
                )
            )
        }
    )


def _room_name(hass, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    if state is None:
        return entity_id
    return state.attributes.get("friendly_name", entity_id)


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
                self._pending_data = user_input
                self._pending_room_weights = {}
                self._room_weight_index = 0
                return await self.async_step_room_weight()

        return self.async_show_form(
            step_id="user",
            data_schema=_data_schema(user_input),
            errors=errors,
        )

    async def async_step_room_weight(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            entity_id = self._pending_data[CONF_ROOM_CLIMATES][self._room_weight_index]
            self._pending_room_weights[entity_id] = user_input[CONF_ROOM_WEIGHT]
            self._room_weight_index += 1

        if self._room_weight_index >= len(self._pending_data[CONF_ROOM_CLIMATES]):
            return self.async_create_entry(
                title=self._pending_data[CONF_TARGET_CLIMATE],
                data={
                    **self._pending_data,
                    CONF_ROOM_WEIGHTS: self._pending_room_weights,
                },
            )

        entity_id = self._pending_data[CONF_ROOM_CLIMATES][self._room_weight_index]
        return self.async_show_form(
            step_id="room_weight",
            data_schema=_room_weight_schema(),
            description_placeholders={"room_name": _room_name(self.hass, entity_id)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AtlanticHeatPumpOptionsFlow:
        """Return the options flow."""
        return AtlanticHeatPumpOptionsFlow()


class AtlanticHeatPumpOptionsFlow(config_entries.OptionsFlow):
    """Allow changing entity selections and modulation values."""

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
                self._pending_data = user_input
                self._pending_room_weights = {}
                self._room_weight_index = 0
                self._existing_room_weights = defaults.get(CONF_ROOM_WEIGHTS, {})
                return await self.async_step_room_weight()

        return self.async_show_form(
            step_id="init",
            data_schema=_data_schema(defaults),
            errors=errors,
        )

    async def async_step_room_weight(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            entity_id = self._pending_data[CONF_ROOM_CLIMATES][self._room_weight_index]
            self._pending_room_weights[entity_id] = user_input[CONF_ROOM_WEIGHT]
            self._room_weight_index += 1

        if self._room_weight_index >= len(self._pending_data[CONF_ROOM_CLIMATES]):
            return self.async_create_entry(
                title="",
                data={
                    **self._pending_data,
                    CONF_ROOM_WEIGHTS: self._pending_room_weights,
                },
            )

        entity_id = self._pending_data[CONF_ROOM_CLIMATES][self._room_weight_index]
        default = self._existing_room_weights.get(entity_id, DEFAULT_ROOM_WEIGHT)
        return self.async_show_form(
            step_id="room_weight",
            data_schema=_room_weight_schema(default),
            description_placeholders={"room_name": _room_name(self.hass, entity_id)},
        )
