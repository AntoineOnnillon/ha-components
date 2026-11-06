"""Move photovoltaic hot-water scheduling and modulation to Home Assistant."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import date, datetime, timedelta
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
    CONF_DAILY_HOUSE_CONSUMPTION_KWH,
    CONF_ELEMENT_POWER_W,
    CONF_ENABLE_SWITCH,
    CONF_EXPORT_VALUE_PCT,
    CONF_FORECAST_MARGIN_PCT,
    CONF_GRID_EXPORT,
    CONF_GRID_IMPORT,
    CONF_GRID_MARGIN_W,
    CONF_HEATER_COMMAND,
    CONF_HEATER_ENERGY,
    CONF_HEATER_POWER,
    CONF_HOUSE_ENERGY,
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
    DEFAULT_DAILY_HOUSE_CONSUMPTION_KWH,
    DEFAULT_EXPORT_VALUE_PCT,
    DEFAULT_FORECAST_MARGIN_PCT,
    DEFAULT_RECOVERY_VALUE_PCT,
    DEFAULT_TANK_LOSS_PCT_PER_DAY,
    DOMAIN,
    ENERGY_HISTORY_DAYS,
    STATE_SAVE_INTERVAL_SECONDS,
    UPDATE_INTERVAL_SECONDS,
    WATER_CP_J_PER_KG_K,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]
_WATER_CP_J_PER_KG_K = WATER_CP_J_PER_KG_K


def _diagnostics_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_diagnostics"


_FORECAST_ENTITIES = (
    (1, CONF_PV_FORECAST),
    (2, CONF_PV_FORECAST_D1),
    (3, CONF_PV_FORECAST_D2),
    (4, CONF_PV_FORECAST_D3),
    (5, CONF_PV_FORECAST_D4),
    (6, CONF_PV_FORECAST_D5),
    (7, CONF_PV_FORECAST_D6),
)


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
        self.config = {
            CONF_EXPORT_VALUE_PCT: DEFAULT_EXPORT_VALUE_PCT,
            CONF_FORECAST_MARGIN_PCT: DEFAULT_FORECAST_MARGIN_PCT,
            CONF_RECOVERY_VALUE_PCT: DEFAULT_RECOVERY_VALUE_PCT,
            CONF_TANK_LOSS_PCT_PER_DAY: DEFAULT_TANK_LOSS_PCT_PER_DAY,
            **config,
        }
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
        self._energy_day = self._budget_day
        self._energy_day_complete = False
        self._energy_last_reading: dict[str, float | None] = {
            "house": None,
            "heater": None,
        }
        self._energy_today_kwh = {"house": 0.0, "heater": 0.0}
        self._energy_seen_today: set[str] = set()
        self._energy_history: list[dict[str, float | str]] = []
        self._warned_energy_units: set[str] = set()
        self.diagnostics: dict[str, float | int | str | None] = {
            "mode": self._last_mode,
            "requested_duty": 0.0,
            "pv_surplus": None,
            "pv_budget": 0.0,
            "pv_surplus_used_today": 0.0,
            "effective_target": None,
            "thermal_charge_percent": None,
            "horizon_available": None,
            "energy_deficit_forecast": None,
            "daily_heater_consumption_forecast": None,
            "peak_window_active": None,
        }

    @staticmethod
    def _finite_float(value: Any) -> float | None:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) else None

    def _value(self, key: str) -> float | None:
        entity_id = self.config.get(key)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        return self._finite_float(state.state)

    def _energy_value_kwh(self, key: str) -> float | None:
        entity_id = self.config.get(key)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        value = self._finite_float(state.state)
        unit = state.attributes.get("unit_of_measurement")
        if value is None:
            return None
        if unit == "Wh":
            return value / 1000.0
        if unit == "kWh":
            return value
        if unit == "MWh":
            return value * 1000.0
        if entity_id not in self._warned_energy_units:
            _LOGGER.warning(
                "Ignoring energy sensor %s with unsupported unit %s; "
                "use Wh, kWh, or MWh",
                entity_id,
                unit,
            )
            self._warned_energy_units.add(entity_id)
        return None

    def _switch_is_on(self, key: str) -> bool | None:
        entity_id = self.config.get(key)
        if not entity_id:
            return None
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
        if isinstance(stored, dict):
            try:
                self._budget_wh = max(0.0, float(stored.get("budget_wh", 0.0)))
                self._surplus_used_today_wh = max(
                    0.0, float(stored.get("surplus_used_today_wh", 0.0))
                )
                self._budget_day = str(stored.get("day", self._budget_day))
                self._energy_day = str(stored.get("energy_day", self._energy_day))
                self._energy_day_complete = bool(
                    stored.get("energy_day_complete", False)
                )
                readings = stored.get("energy_last_reading", {})
                daily = stored.get("energy_today_kwh", {})
                if not isinstance(readings, dict):
                    readings = {}
                if not isinstance(daily, dict):
                    daily = {}
                self._energy_last_reading = {
                    key: self._finite_float(readings.get(key))
                    for key in ("house", "heater")
                }
                self._energy_today_kwh = {
                    key: max(0.0, self._finite_float(daily.get(key)) or 0.0)
                    for key in ("house", "heater")
                }
                seen_today = stored.get("energy_seen_today", [])
                if not isinstance(seen_today, list):
                    seen_today = []
                self._energy_seen_today = set(seen_today) & {"house", "heater"}
                history = stored.get("energy_history", [])
                if isinstance(history, list):
                    self._energy_history = []
                    for item in history:
                        if not isinstance(item, dict):
                            continue
                        record: dict[str, float | str] = {}
                        if isinstance(item.get("day"), str):
                            record["day"] = item["day"]
                        for key in ("house", "heater"):
                            value = self._finite_float(item.get(key))
                            if value is not None and value >= 0:
                                record[key] = value
                        if len(record) > 1:
                            self._energy_history.append(record)
                    self._energy_history = self._energy_history[-ENERGY_HISTORY_DAYS:]
            except (TypeError, ValueError):
                _LOGGER.warning("Ignoring invalid stored Cumulus PV reserve")
        self._reset_daily_counter()
        self._roll_energy_day(dt_util.now().date().isoformat())
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
                    CONF_HOUSE_ENERGY,
                    CONF_HEATER_ENERGY,
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

    def _stored_state(self) -> dict[str, Any]:
        return {
            "budget_wh": self._budget_wh,
            "surplus_used_today_wh": self._surplus_used_today_wh,
            "day": self._budget_day,
            "energy_day": self._energy_day,
            "energy_day_complete": self._energy_day_complete,
            "energy_last_reading": self._energy_last_reading,
            "energy_today_kwh": self._energy_today_kwh,
            "energy_seen_today": sorted(self._energy_seen_today),
            "energy_history": self._energy_history,
        }

    def _roll_energy_day(self, today: str) -> None:
        if today == self._energy_day:
            return
        try:
            consecutive_day = (
                date.fromisoformat(today) - date.fromisoformat(self._energy_day)
            ).days == 1
        except ValueError:
            consecutive_day = False
        if self._energy_day_complete and consecutive_day and self._energy_seen_today:
            record: dict[str, float | str] = {"day": self._energy_day}
            record.update(
                {
                    key: self._energy_today_kwh[key]
                    for key in self._energy_seen_today
                }
            )
            self._energy_history.append(record)
            self._energy_history = self._energy_history[-ENERGY_HISTORY_DAYS:]
        self._energy_day = today
        self._energy_day_complete = consecutive_day
        self._energy_today_kwh = {"house": 0.0, "heater": 0.0}
        self._energy_seen_today.clear()

    def _track_energy_usage(self, now: datetime, dt_seconds: float) -> None:
        self._roll_energy_day(dt_util.as_local(now).date().isoformat())
        for kind, config_key in (("house", CONF_HOUSE_ENERGY),):
            reading = self._energy_value_kwh(config_key)
            if reading is None:
                continue
            previous = self._energy_last_reading[kind]
            if previous is not None:
                delta = reading - previous
                self._energy_today_kwh[kind] += (
                    delta if delta >= 0 else reading
                )
                self._energy_seen_today.add(kind)
            self._energy_last_reading[kind] = reading

        heater_reading = self._energy_value_kwh(CONF_HEATER_ENERGY)
        if heater_reading is not None:
            previous = self._energy_last_reading["heater"]
            if previous is not None:
                delta = heater_reading - previous
                self._energy_today_kwh["heater"] += (
                    delta if delta >= 0 else heater_reading
                )
            self._energy_last_reading["heater"] = heater_reading
            self._energy_seen_today.add("heater")
            return

        if self.config.get(CONF_HEATER_ENERGY):
            return

        if self._switch_is_on(CONF_ENABLE_SWITCH) is not True:
            return

        heater_power = self._value(CONF_HEATER_POWER)
        if heater_power is None:
            boost = self._switch_is_on(CONF_BOOST_SWITCH)
            priority = self._switch_is_on(CONF_PV_PRIORITY_SWITCH)
            if boost:
                heater_power = self.config[CONF_ELEMENT_POWER_W]
            elif priority:
                heater_power = self.config[CONF_ELEMENT_POWER_W] * self._last_duty
        if heater_power is None or dt_seconds <= 0:
            return
        self._energy_today_kwh["heater"] += (
            max(heater_power, 0.0) * dt_seconds / 3_600_000.0
        )
        self._energy_seen_today.add("heater")

    def _average_daily_energy(self, kind: str) -> float | None:
        values = [
            record[kind]
            for record in self._energy_history
            if kind in record and isinstance(record[kind], (int, float))
        ]
        return sum(values) / len(values) if values else None

    def _daily_energy_estimates(self) -> tuple[float, float]:
        heater = self._average_daily_energy("heater")
        if heater is None:
            heater = self.config.get(CONF_DAILY_CONSUMPTION_KWH, 8.0)

        house_total = self._average_daily_energy("house")
        if (
            self.config.get(CONF_HOUSE_ENERGY)
            and self.config.get(CONF_HEATER_ENERGY)
        ):
            paired_days = [
                max(record["house"] - record["heater"], 0.0)
                for record in self._energy_history
                if "house" in record and "heater" in record
            ]
            house = (
                sum(paired_days) / len(paired_days)
                if paired_days
                else self.config.get(
                    CONF_DAILY_HOUSE_CONSUMPTION_KWH,
                    DEFAULT_DAILY_HOUSE_CONSUMPTION_KWH,
                )
            )
        elif house_total is None:
            house = self.config.get(
                CONF_DAILY_HOUSE_CONSUMPTION_KWH,
                DEFAULT_DAILY_HOUSE_CONSUMPTION_KWH,
            )
        else:
            house = max(0.0, house_total - heater)
        return house, heater

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
            import_entity = self.config.get(CONF_GRID_IMPORT)
            imported = self._value(CONF_GRID_IMPORT) if import_entity else 0.0
            if imported is None:
                return None
            return export - imported
        return self._value(CONF_PV_SURPLUS)

    def _forecast_plan(self) -> tuple[float | None, float | None, int | None]:
        house_daily, heater_daily = self._daily_energy_estimates()
        forecast_days: list[tuple[int, float, float]] = []
        forecast_margin = self.config[CONF_FORECAST_MARGIN_PCT] / 100.0
        for day, key in _FORECAST_ENTITIES:
            if day > self.config[CONF_HORIZON_DAYS]:
                break
            forecast = self._value(key)
            if forecast is None:
                continue
            if day == 1:
                house_remaining = self._value(CONF_CONSUMPTION_FORECAST)
                if house_remaining is None:
                    local_now = dt_util.as_local(dt_util.now())
                    elapsed_hours = local_now.hour + local_now.minute / 60.0
                    house_remaining = house_daily * (24.0 - elapsed_hours) / 24.0
                house_need = max(house_remaining, 0.0)
            else:
                house_need = house_daily
            heater_need = heater_daily
            if day == 1 and "heater" in self._energy_seen_today:
                heater_need = max(
                    0.0,
                    heater_daily - self._energy_today_kwh["heater"],
                )
            conservative_forecast = max(forecast, 0.0) * (
                1.0 - forecast_margin
            )
            pv_after_house = max(conservative_forecast - house_need, 0.0)
            forecast_days.append(
                (day, pv_after_house - heater_need, pv_after_house)
            )

        if not forecast_days:
            return None, None, None

        available = sum(pv_after_house for _, _, pv_after_house in forecast_days)
        retention = 1.0 - self.config[CONF_TANK_LOSS_PCT_PER_DAY] / 100.0
        reserve_needed = 0.0
        reserve_day: int | None = None
        segment: list[tuple[int, float]] = []

        def reserve_for_segment(
            days: list[tuple[int, float]],
        ) -> tuple[float, int | None]:
            if not days:
                return 0.0, None
            balance = 0.0
            peak_balance = 0.0
            reserve = 0.0
            reserve_day: int | None = None
            start_day = days[0][0]
            previous_day = start_day
            for index, (day, net_energy) in enumerate(days):
                if index:
                    balance *= retention ** (day - previous_day)
                balance += net_energy
                peak_balance = max(peak_balance, balance)
                drawdown = peak_balance - balance
                if drawdown > reserve:
                    reserve = drawdown
                    reserve_day = day
                previous_day = day
            if start_day > 1:
                reserve /= retention ** (start_day - 1)
            return reserve, reserve_day

        previous_day: int | None = None
        for day, net_energy, _ in forecast_days:
            if previous_day is not None and day != previous_day + 1:
                segment_reserve, segment_day = reserve_for_segment(segment)
                if segment_reserve > reserve_needed:
                    reserve_needed = segment_reserve
                    reserve_day = segment_day
                segment = []
            segment.append((day, net_energy))
            previous_day = day
        segment_reserve, segment_day = reserve_for_segment(segment)
        if segment_reserve > reserve_needed:
            reserve_needed = segment_reserve
            reserve_day = segment_day
        return available, reserve_needed, reserve_day

    def _effective_target_c(
        self,
    ) -> tuple[float, float, float | None, float | None]:
        target = self.config[CONF_TARGET_C]
        if not self.config[CONF_PLANNER_ENABLED]:
            return target, 0.0, None, None

        available, reserve_needed, reserve_day = self._forecast_plan()
        if reserve_needed is None or available is None:
            return target, 0.0, None, None

        tank_capacity_per_degree = (
            self.config[CONF_TANK_VOLUME_L]
            * _WATER_CP_J_PER_KG_K
            / 3_600_000.0
        )
        if tank_capacity_per_degree <= 0:
            return target, 0.0, available, reserve_needed
        retention = 1.0 - self.config[CONF_TANK_LOSS_PCT_PER_DAY] / 100.0
        recovery_value = (
            self.config[CONF_RECOVERY_VALUE_PCT]
            / 100.0
            * retention ** max((reserve_day or 1) - 1, 0)
        )
        export_value = self.config[CONF_EXPORT_VALUE_PCT] / 100.0
        if recovery_value <= export_value:
            return target, 0.0, available, reserve_needed
        offset = min(
            self.config[CONF_BOOST_OFFSET_MAX_C],
            reserve_needed / tank_capacity_per_degree,
        )
        return (
            min(
                self.config[CONF_MAX_TEMP_C],
                max(self.config[CONF_MIN_TEMP_C], target + offset),
            ),
            offset,
            available,
            reserve_needed,
        )

    def _thermal_charge_percent(self, top: float) -> float:
        """Estimate stored heat relative to the minimum and base targets."""
        minimum = self.config[CONF_MIN_TEMP_C]
        base = self.config[CONF_TARGET_C]
        if base <= minimum:
            return 0.0
        boost_limit = min(
            self.config[CONF_MAX_TEMP_C],
            base + self.config[CONF_BOOST_OFFSET_MAX_C],
        )
        if top <= minimum:
            return 0.0
        if top <= base:
            return min(100.0, (top - minimum) / (base - minimum) * 100.0)
        if boost_limit <= base:
            return 100.0
        return min(
            150.0,
            100.0
            + (top - base) / (boost_limit - base) * 50.0,
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
        (
            target,
            _offset,
            horizon_available,
            forecast_reserve,
        ) = self._effective_target_c()
        surplus = self._effective_surplus_w()
        self._set_diagnostics(
            pv_surplus=surplus,
            effective_target=target,
            horizon_available=horizon_available,
            energy_deficit_forecast=forecast_reserve,
        )

        self._reset_daily_counter()
        if surplus is not None and surplus > 0 and dt_seconds > 0:
            self._budget_wh += surplus * dt_seconds / 3600.0

        if top < self.config[CONF_MIN_TEMP_C] - self.config[CONF_HYSTERESIS_C]:
            return 1.0, "minimum_temperature", target, surplus
        if top >= target:
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
            now = dt_util.now()
            self._track_energy_usage(now, dt_seconds)
            _, daily_heater_forecast = self._daily_energy_estimates()
            self._set_diagnostics(
                daily_heater_consumption_forecast=daily_heater_forecast
            )

            top = self._value(CONF_TOP_TEMPERATURE)
            if top is None:
                _LOGGER.warning(
                    "Cannot update the Cumulus command: %s has no valid temperature",
                    self.config[CONF_TOP_TEMPERATURE],
                )
                self._set_diagnostics(mode="temperature_unavailable")
                return
            self._set_diagnostics(
                thermal_charge_percent=self._thermal_charge_percent(top)
            )

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
                    top, now, dt_seconds
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
