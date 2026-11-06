"""Configuration flow for the Cumulus PV controller."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_BOOST_OFFSET_MAX_C,
    CONF_BOOST_SWITCH,
    CONF_BURST_HYSTERESIS_WH,
    CONF_CONSUMPTION_FORECAST,
    CONF_DAILY_CONSUMPTION_KWH,
    CONF_DAILY_HOUSE_CONSUMPTION_KWH,
    CONF_ELEMENT_POWER_W,
    CONF_ENABLE_SWITCH,
    CONF_EXPORT_VALUE_PCT,
    CONF_FORECAST_MARGIN_PCT,
    CONF_GRID_EXPORT,
    CONF_GRID_IMPORT,
    CONF_GRID_MARGIN_W,
    CONF_HOUSE_ENERGY,
    CONF_HEATER_ENERGY,
    CONF_HEATER_COMMAND,
    CONF_HEATER_POWER,
    CONF_HORIZON_DAYS,
    CONF_HYSTERESIS_C,
    CONF_MAX_TEMP_C,
    CONF_MIN_TEMP_C,
    CONF_PEAK_END_H,
    CONF_PEAK_HOUR,
    CONF_PEAK_PAD_AFTER_H,
    CONF_PEAK_PAD_BEFORE_H,
    CONF_PEAK_START_H,
    CONF_PLANNER_ENABLED,
    CONF_PREFER_BURST,
    CONF_RECOVERY_VALUE_PCT,
    CONF_PV_FORECAST,
    CONF_PV_FORECAST_D1,
    CONF_PV_FORECAST_D2,
    CONF_PV_FORECAST_D3,
    CONF_PV_FORECAST_D4,
    CONF_PV_FORECAST_D5,
    CONF_PV_FORECAST_D6,
    CONF_PV_PRIORITY_SWITCH,
    CONF_PV_SURPLUS,
    CONF_SURPLUS_THRESHOLD_W,
    CONF_TANK_VOLUME_L,
    CONF_TANK_LOSS_PCT_PER_DAY,
    CONF_TARGET_C,
    CONF_TOP_TEMPERATURE,
    CONF_WAIT_FOR_PEAK,
    DEFAULT_BOOST_OFFSET_MAX_C,
    DEFAULT_BURST_HYSTERESIS_WH,
    DEFAULT_DAILY_CONSUMPTION_KWH,
    DEFAULT_DAILY_HOUSE_CONSUMPTION_KWH,
    DEFAULT_ELEMENT_POWER_W,
    DEFAULT_EXPORT_VALUE_PCT,
    DEFAULT_FORECAST_MARGIN_PCT,
    DEFAULT_GRID_MARGIN_W,
    DEFAULT_HORIZON_DAYS,
    DEFAULT_HYSTERESIS_C,
    DEFAULT_MAX_TEMP_C,
    DEFAULT_MIN_TEMP_C,
    DEFAULT_PEAK_END_H,
    DEFAULT_PEAK_PAD_AFTER_H,
    DEFAULT_PEAK_PAD_BEFORE_H,
    DEFAULT_PEAK_START_H,
    DEFAULT_PLANNER_ENABLED,
    DEFAULT_PREFER_BURST,
    DEFAULT_RECOVERY_VALUE_PCT,
    DEFAULT_SURPLUS_THRESHOLD_W,
    DEFAULT_TANK_VOLUME_L,
    DEFAULT_TANK_LOSS_PCT_PER_DAY,
    DEFAULT_TARGET_C,
    DEFAULT_WAIT_FOR_PEAK,
    DOMAIN,
)


ENTITY_FIELDS: tuple[tuple[str, str, bool], ...] = (
    (CONF_TOP_TEMPERATURE, "sensor", True),
    (CONF_HEATER_COMMAND, "number", True),
    (CONF_PV_PRIORITY_SWITCH, "switch", True),
    (CONF_ENABLE_SWITCH, "switch", True),
    (CONF_BOOST_SWITCH, "switch", True),
    (CONF_PV_SURPLUS, "sensor", False),
    (CONF_GRID_EXPORT, "sensor", False),
    (CONF_GRID_IMPORT, "sensor", False),
    (CONF_PV_FORECAST, "sensor", False),
    (CONF_PV_FORECAST_D1, "sensor", False),
    (CONF_PV_FORECAST_D2, "sensor", False),
    (CONF_PV_FORECAST_D3, "sensor", False),
    (CONF_PV_FORECAST_D4, "sensor", False),
    (CONF_PV_FORECAST_D5, "sensor", False),
    (CONF_PV_FORECAST_D6, "sensor", False),
    (CONF_CONSUMPTION_FORECAST, "sensor", False),
    (CONF_HOUSE_ENERGY, "sensor", False),
    (CONF_HEATER_ENERGY, "sensor", False),
    (CONF_PEAK_HOUR, "sensor", False),
    (CONF_HEATER_POWER, "sensor", False),
)

NUMBER_FIELDS: tuple[tuple[str, float, float, float, float], ...] = (
    (CONF_TANK_VOLUME_L, 10, 1000, 10, DEFAULT_TANK_VOLUME_L),
    (CONF_ELEMENT_POWER_W, 200, 10000, 100, DEFAULT_ELEMENT_POWER_W),
    (CONF_TARGET_C, 30, 75, 0.5, DEFAULT_TARGET_C),
    (CONF_MIN_TEMP_C, 20, 60, 0.5, DEFAULT_MIN_TEMP_C),
    (CONF_MAX_TEMP_C, 40, 85, 0.5, DEFAULT_MAX_TEMP_C),
    (CONF_HYSTERESIS_C, 0.5, 15, 0.5, DEFAULT_HYSTERESIS_C),
    (CONF_SURPLUS_THRESHOLD_W, 0, 5000, 10, DEFAULT_SURPLUS_THRESHOLD_W),
    (CONF_BURST_HYSTERESIS_WH, 0, 5000, 10, DEFAULT_BURST_HYSTERESIS_WH),
    (CONF_GRID_MARGIN_W, 0, 1000, 10, DEFAULT_GRID_MARGIN_W),
    (CONF_PEAK_START_H, 0, 23, 1, DEFAULT_PEAK_START_H),
    (CONF_PEAK_END_H, 1, 24, 1, DEFAULT_PEAK_END_H),
    (CONF_PEAK_PAD_BEFORE_H, 0, 6, 1, DEFAULT_PEAK_PAD_BEFORE_H),
    (CONF_PEAK_PAD_AFTER_H, 0, 6, 1, DEFAULT_PEAK_PAD_AFTER_H),
    (CONF_DAILY_CONSUMPTION_KWH, 0, 50, 0.5, DEFAULT_DAILY_CONSUMPTION_KWH),
    (
        CONF_DAILY_HOUSE_CONSUMPTION_KWH,
        0,
        100,
        0.5,
        DEFAULT_DAILY_HOUSE_CONSUMPTION_KWH,
    ),
    (CONF_HORIZON_DAYS, 1, 7, 1, DEFAULT_HORIZON_DAYS),
    (CONF_BOOST_OFFSET_MAX_C, 0, 20, 0.5, DEFAULT_BOOST_OFFSET_MAX_C),
    (CONF_FORECAST_MARGIN_PCT, 0, 50, 1, DEFAULT_FORECAST_MARGIN_PCT),
    (
        CONF_TANK_LOSS_PCT_PER_DAY,
        0,
        30,
        1,
        DEFAULT_TANK_LOSS_PCT_PER_DAY,
    ),
    (CONF_RECOVERY_VALUE_PCT, 0, 100, 1, DEFAULT_RECOVERY_VALUE_PCT),
    (CONF_EXPORT_VALUE_PCT, 0, 100, 1, DEFAULT_EXPORT_VALUE_PCT),
)

BOOLEAN_FIELDS: tuple[tuple[str, bool], ...] = (
    (CONF_PREFER_BURST, DEFAULT_PREFER_BURST),
    (CONF_PLANNER_ENABLED, DEFAULT_PLANNER_ENABLED),
    (CONF_WAIT_FOR_PEAK, DEFAULT_WAIT_FOR_PEAK),
)


def _data_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    schema: dict[Any, Any] = {}

    for key, domain, required in ENTITY_FIELDS:
        selector = EntitySelector(EntitySelectorConfig(domain=domain))
        if required:
            marker = (
                vol.Required(key, default=defaults[key])
                if key in defaults
                else vol.Required(key)
            )
        else:
            marker = (
                vol.Optional(key, default=defaults[key])
                if key in defaults and defaults[key] is not None
                else vol.Optional(key)
            )
        schema[marker] = selector

    for key, minimum, maximum, step, default in NUMBER_FIELDS:
        schema[
            vol.Required(key, default=defaults.get(key, default))
        ] = NumberSelector(
            NumberSelectorConfig(
                min=minimum,
                max=maximum,
                step=step,
                mode=NumberSelectorMode.BOX,
            )
        )

    for key, default in BOOLEAN_FIELDS:
        schema[vol.Required(key, default=defaults.get(key, default))] = (
            BooleanSelector()
        )

    return vol.Schema(schema)


def _validate_input(user_input: dict[str, Any]) -> str | None:
    if not user_input.get(CONF_PV_SURPLUS) and not user_input.get(CONF_GRID_EXPORT):
        return "no_surplus_source"
    if not (
        user_input[CONF_MIN_TEMP_C]
        < user_input[CONF_TARGET_C]
        <= user_input[CONF_MAX_TEMP_C]
    ):
        return "invalid_temperature_limits"
    if user_input[CONF_PEAK_START_H] == user_input[CONF_PEAK_END_H]:
        return "invalid_peak_window"
    return None


class CumulusPVConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up a controller that calculates PV heating demand in Home Assistant."""

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
                await self.async_set_unique_id(user_input[CONF_HEATER_COMMAND])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_HEATER_COMMAND],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_data_schema(user_input),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> CumulusPVOptionsFlow:
        """Return the options flow."""
        return CumulusPVOptionsFlow()


class CumulusPVOptionsFlow(config_entries.OptionsFlow):
    """Change the configured entities and PV strategy."""

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
