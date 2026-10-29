"""Move photovoltaic hot-water scheduling and modulation to Home Assistant."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BOOST_OFFSET_MAX_C,
    CONF_BOOST_SWITCH,
    CONF_BURST_HYSTERESIS_WH,
    CONF_CONSUMPTION_FORECAST,
    CONF_DAILY_CONSUMPTION_KWH,
    CONF_DEFICIT_OFFSET_MAX_C,
    CONF_ELEMENT_POWER_W,
    CONF_ENABLE_SWITCH,
    CONF_GRID_EXPORT,
    CONF_GRID_IMPORT,
    CONF_GRID_MARGIN_W,
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
    CONF_PV_FORECAST,
    CONF_PV_FORECAST_D1,
    CONF_PV_FORECAST_D2,
    CONF_PV_PRIORITY_SWITCH,
    CONF_PV_SURPLUS,
    CONF_SURPLUS_THRESHOLD_W,
    CONF_TANK_VOLUME_L,
    CONF_TARGET_C,
    CONF_TOP_TEMPERATURE,
    CONF_WAIT_FOR_PEAK,
    DOMAIN,
    STATE_SAVE_INTERVAL_SECONDS,
    UPDATE_INTERVAL_SECONDS,
    WATER_CP_J_PER_KG_K,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]
_WATER_CP_J_PER_KG_K = WATER_CP_J_PER_KG_K


def _diagnostics_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_diagnostics"


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one Cumulus PV controller."""
    controller = CumulusPVController(
        hass, entry.entry_id, {**entry.data, **entry.options}
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = controller
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    await controller.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after changing integration options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a configured controller."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    controller: CumulusPVController = hass.data[DOMAIN].pop(entry.entry_id)
    await controller.async_stop()
    return True


class CumulusPVController:
    """Calculate heater duty in HA and leave physical safety to ESPHome."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, config: dict[str, Any]
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.config = config
        self._lock = asyncio.Lock()
        self._unsub_state: Any = None
        self._unsub_interval: Any = None
        self._store = Store(hass, 1, f"{DOMAIN}_{entry_id}")
        self._last_update = 0.0
        self._last_save = 0.0
        self._last_duty = 0.0
        self._last_mode = "starting"
        self._budget_wh = 0.0
        self._surplus_used_today_wh = 0.0
        self._budget_day = dt_util.now().date().isoformat()
        self.diagnostics: dict[str, float | int | str | None] = {
            "mode": self._last_mode,
            "requested_duty": 0.0,
            "pv_surplus": None,
            "pv_budget": 0.0,
            "pv_surplus_used_today": 0.0,
            "effective_target": None,
            "horizon_available": None,
            "energy_deficit_forecast": None,
            "peak_window_active": None,
        }

    def _value(self, key: str) -> float | None:
        entity_id = self.config.get(key)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            result = float(state.state)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) else None

    def _switch_is_on(self, key: str) -> bool | None:
        entity_id = self.config[key]
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        return state.state == "on"

    def _set_diagnostics(self, **values: float | int | str | None) -> None:
        if all(self.diagnostics.get(key) == value for key, value in values.items()):
            return
        self.diagnostics.update(values)
        async_dispatcher_send(self.hass, _diagnostics_signal(self.entry_id))

    async def async_start(self) -> None:
        """Restore the PV reserve, enable HA control, and start updates."""
        stored = await self._store.async_load()
        if stored:
            try:
                self._budget_wh = max(0.0, float(stored.get("budget_wh", 0.0)))
                self._surplus_used_today_wh = max(
                    0.0, float(stored.get("surplus_used_today_wh", 0.0))
                )
                self._budget_day = str(stored.get("day", self._budget_day))
            except (TypeError, ValueError):
                _LOGGER.warning("Ignoring invalid stored Cumulus PV reserve")
        self._reset_daily_counter()
        await self._async_set_switch(CONF_PV_PRIORITY_SWITCH, True)

        watched = list(
            dict.fromkeys(
                entity_id
                for key, value in self.config.items()
                if key in {
                    CONF_TOP_TEMPERATURE,
                    CONF_PV_PRIORITY_SWITCH,
                    CONF_ENABLE_SWITCH,
                    CONF_BOOST_SWITCH,
                }
                or key.startswith(
                    (
                        "pv_",
                        "grid_",
                        "consumption_",
                        "peak_",
                        "heater_power",
                    )
                )
                if isinstance(value, str) and "." in value
            )
        )
        self._unsub_state = async_track_state_change_event(
            self.hass, watched, self._handle_state_change
        )
        self._unsub_interval = async_track_time_interval(
            self.hass,
            self._handle_interval,
            timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        await self.async_update()

    async def async_stop(self) -> None:
        """Stop updates and return the ESPHome controller to local mode."""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
        await self._async_set_switch(CONF_PV_PRIORITY_SWITCH, False)
        await self._store.async_save(self._stored_state())

    def _stored_state(self) -> dict[str, float | str]:
        return {
            "budget_wh": self._budget_wh,
            "surplus_used_today_wh": self._surplus_used_today_wh,
            "day": self._budget_day,
        }

    @callback
    def _handle_state_change(self, event: Event) -> None:
        self.hass.async_create_task(self.async_update())

    @callback
    def _handle_interval(self, now: datetime) -> None:
        self.hass.async_create_task(self.async_update())

    def _reset_daily_counter(self) -> None:
        today = dt_util.now().date().isoformat()
        if today != self._budget_day:
            self._budget_day = today
            self._surplus_used_today_wh = 0.0

    def _effective_surplus_w(self) -> float | None:
        export = self._value(CONF_GRID_EXPORT)
        if export is not None:
            imported = self._value(CONF_GRID_IMPORT)
            return export - (imported or 0.0)
        return self._value(CONF_PV_SURPLUS)

    def _compute_horizon_available_kwh(self) -> float:
        total = 0.0
        forecast_today = self._value(CONF_PV_FORECAST)
        if forecast_today is not None:
            consumption = self._value(CONF_CONSUMPTION_FORECAST)
            total += max(
                forecast_today - (consumption or 0.0),
                0.0,
            )

        for day_index, key in (
            (2, CONF_PV_FORECAST_D1),
            (3, CONF_PV_FORECAST_D2),
        ):
            if self.config[CONF_HORIZON_DAYS] >= day_index:
                forecast = self._value(key)
                if forecast is not None:
                    total += max(
                        forecast - self.config[CONF_DAILY_CONSUMPTION_KWH],
                        0.0,
                    )
        return total

    def _effective_target_c(self) -> tuple[float, float, float | None]:
        target = self.config[CONF_TARGET_C]
        if not self.config[CONF_PLANNER_ENABLED]:
            return target, 0.0, None

        has_forecast = any(
            self._value(key) is not None
            for key in (CONF_PV_FORECAST, CONF_PV_FORECAST_D1, CONF_PV_FORECAST_D2)
        )
        if not has_forecast:
            return target, 0.0, None

        available = self._compute_horizon_available_kwh()
        needed = (
            self.config[CONF_DAILY_CONSUMPTION_KWH]
            * self.config[CONF_HORIZON_DAYS]
        )
        if needed <= 0.1:
            return target, 0.0, available

        ratio = available / needed
        if ratio <= 1.0:
            offset = min(
                self.config[CONF_BOOST_OFFSET_MAX_C] * (1.0 - ratio),
                self.config[CONF_BOOST_OFFSET_MAX_C],
            )
        else:
            offset = -self.config[CONF_DEFICIT_OFFSET_MAX_C] * min(
                ratio - 1.0, 1.0
            )
        return (
            min(
                self.config[CONF_MAX_TEMP_C],
                max(self.config[CONF_MIN_TEMP_C], target + offset),
            ),
            offset,
            available,
        )

    def _in_peak_window(self, now: datetime) -> bool:
        start = self.config[CONF_PEAK_START_H]
        end = self.config[CONF_PEAK_END_H]
        peak_hour = self._value(CONF_PEAK_HOUR)
        if peak_hour is not None:
            peak = math.floor(peak_hour + 0.5)
            start = peak - self.config[CONF_PEAK_PAD_BEFORE_H]
            end = peak + self.config[CONF_PEAK_PAD_AFTER_H]
        start = max(start, 0)
        end = min(end, 24)
        hour = dt_util.as_local(now).hour
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    def _thermostat_duty(self, top: float) -> float:
        target = self.config[CONF_TARGET_C]
        if self._last_duty > 0.5:
            return 1.0 if top < target else 0.0
        return 1.0 if top < target - self.config[CONF_HYSTERESIS_C] else 0.0

    def _compute_duty(
        self, top: float, now: datetime, dt_seconds: float
    ) -> tuple[float, str, float, float | None]:
        target, _offset, horizon_available = self._effective_target_c()
        surplus = self._effective_surplus_w()
        self._set_diagnostics(
            pv_surplus=surplus,
            effective_target=target,
            horizon_available=horizon_available,
            energy_deficit_forecast=(
                self.config[CONF_DAILY_CONSUMPTION_KWH]
                * self.config[CONF_HORIZON_DAYS]
                - horizon_available
                if horizon_available is not None
                else None
            ),
        )

        self._reset_daily_counter()
        if surplus is not None and surplus > 0 and dt_seconds > 0:
            self._budget_wh += surplus * dt_seconds / 3600.0

        if top < self.config[CONF_MIN_TEMP_C] - self.config[CONF_HYSTERESIS_C]:
            return 1.0, "minimum_temperature", target, surplus
        if top >= target + self.config[CONF_HYSTERESIS_C]:
            return 0.0, "target_reached", target, surplus

        if surplus is None:
            return self._thermostat_duty(top), "thermostat_no_pv_data", target, None

        if self.config[CONF_PLANNER_ENABLED] and self.config[CONF_WAIT_FOR_PEAK]:
            in_window = self._in_peak_window(now)
            self._set_diagnostics(peak_window_active=int(in_window))
            if not in_window:
                end = self.config[CONF_PEAK_END_H]
                peak_hour = self._value(CONF_PEAK_HOUR)
                if peak_hour is not None:
                    end = min(
                        24,
                        math.floor(peak_hour + 0.5)
                        + self.config[CONF_PEAK_PAD_AFTER_H],
                    )
                if dt_util.as_local(now).hour >= end and top < (
                    self.config[CONF_TARGET_C] - self.config[CONF_HYSTERESIS_C]
                ):
                    return self._thermostat_duty(top), "thermostat_catchup", target, surplus
                return 0.0, "pv_wait", target, surplus
        else:
            self._set_diagnostics(peak_window_active=None)

        if self.config[CONF_PREFER_BURST]:
            needed_wh = (
                self.config[CONF_TANK_VOLUME_L]
                * _WATER_CP_J_PER_KG_K
                * (target - top)
                / 3600.0
            )
            budget_ready = self._budget_wh >= (
                needed_wh + self.config[CONF_BURST_HYSTERESIS_WH]
            )
            already_heating = self._last_duty > 0.5
            duty = (
                1.0
                if budget_ready or (already_heating and self._budget_wh > 0)
                else 0.0
            )
            return duty, "pv_eco", target, surplus

        heater_power = self._value(CONF_HEATER_POWER)
        grid_export = self._value(CONF_GRID_EXPORT)
        if heater_power is not None and grid_export is not None:
            duty = (
                heater_power + surplus - self.config[CONF_GRID_MARGIN_W]
            ) / self.config[CONF_ELEMENT_POWER_W]
        else:
            duty = (
                surplus / self.config[CONF_ELEMENT_POWER_W]
                if surplus >= self.config[CONF_SURPLUS_THRESHOLD_W]
                else 0.0
            )
        return min(1.0, max(0.0, duty)), "pv_mod", target, surplus

    async def _async_set_switch(self, key: str, enabled: bool) -> None:
        entity_id = self.config[key]
        state = self.hass.states.get(entity_id)
        if state is not None and state.state == ("on" if enabled else "off"):
            return
        service = "turn_on" if enabled else "turn_off"
        try:
            await self.hass.services.async_call(
                "switch",
                service,
                {"entity_id": entity_id},
                blocking=True,
            )
        except HomeAssistantError:
            _LOGGER.exception("Failed to %s %s", service, entity_id)

    async def _async_set_duty(self, duty: float) -> None:
        entity_id = self.config[CONF_HEATER_COMMAND]
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity_id, "value": round(duty * 100.0)},
                blocking=True,
            )
        except HomeAssistantError:
            _LOGGER.exception("Failed to send heater command to %s", entity_id)

    async def async_update(self) -> None:
        """Calculate and send a fresh command, allowing the ESP watchdog to work."""
        async with self._lock:
            now_monotonic = time.monotonic()
            dt_seconds = (
                min(now_monotonic - self._last_update, 60.0)
                if self._last_update
                else 0.0
            )
            self._last_update = now_monotonic

            top = self._value(CONF_TOP_TEMPERATURE)
            if top is None:
                _LOGGER.warning(
                    "Cannot update the Cumulus command: %s has no valid temperature",
                    self.config[CONF_TOP_TEMPERATURE],
                )
                self._set_diagnostics(mode="temperature_unavailable")
                return

            enabled = self._switch_is_on(CONF_ENABLE_SWITCH)
            priority = self._switch_is_on(CONF_PV_PRIORITY_SWITCH)
            boost = self._switch_is_on(CONF_BOOST_SWITCH)
            if enabled is None or priority is None or boost is None:
                _LOGGER.warning("Cumulus control switches are unavailable")
                self._set_diagnostics(mode="control_unavailable")
                return
            if not priority:
                self._set_diagnostics(mode="local_thermostat")
                return

            if not enabled:
                duty, mode, _target, surplus = 0.0, "disabled", None, None
            else:
                duty, mode, _target, surplus = self._compute_duty(
                    top, dt_util.now(), dt_seconds
                )

            await self._async_set_duty(duty)
            self._last_duty = duty
            self._last_mode = mode
            self._set_diagnostics(
                mode=mode,
                requested_duty=duty * 100.0,
                pv_budget=self._budget_wh / 1000.0,
                pv_surplus_used_today=self._surplus_used_today_wh / 1000.0,
            )

            if enabled and not boost and duty > 0:
                measured_power = self._value(CONF_HEATER_POWER)
                power = (
                    measured_power
                    if measured_power is not None
                    else self.config[CONF_ELEMENT_POWER_W] * duty
                )
                consumed_wh = max(0.0, power) * dt_seconds / 3600.0
                if mode in ("pv_eco", "pv_mod"):
                    self._budget_wh = max(0.0, self._budget_wh - consumed_wh)
                    if surplus is not None:
                        self._surplus_used_today_wh += min(
                            consumed_wh,
                            max(0.0, surplus) * dt_seconds / 3600.0,
                        )

            if (
                now_monotonic - self._last_save >= STATE_SAVE_INTERVAL_SECONDS
            ):
                await self._store.async_save(self._stored_state())
                self._last_save = now_monotonic


__all__ = [
    "async_setup",
    "async_setup_entry",
    "async_unload_entry",
    "async_reload_entry",
]
