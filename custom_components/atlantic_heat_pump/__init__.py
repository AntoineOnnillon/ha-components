"""Control an Atlantic heat-pump climate from Home Assistant room climates."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_DEADBAND,
    CONF_DELTA_MAX,
    CONF_ROOM_CLIMATES,
    CONF_TARGET_CLIMATE,
    DOMAIN,
    DEFAULT_DEADBAND,
    DEFAULT_DELTA_MAX,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a configured controller."""
    config = {**entry.data, **entry.options}
    controller = AtlanticHeatPumpController(hass, config)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = controller
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    await controller.async_start()
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the controller when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a configured controller."""
    controller: AtlanticHeatPumpController = hass.data[DOMAIN].pop(entry.entry_id)
    controller.async_stop()
    return True


class AtlanticHeatPumpController:
    """Apply room demand to the Atlantic climate entity."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self.target_climate: str = config[CONF_TARGET_CLIMATE]
        self.room_climates: list[str] = config[CONF_ROOM_CLIMATES]
        self.deadband: float = float(config.get(CONF_DEADBAND, DEFAULT_DEADBAND))
        self.delta_max: float = float(config.get(CONF_DELTA_MAX, DEFAULT_DELTA_MAX))
        self._lock = asyncio.Lock()
        self._unsubscribe: Callable[[], None] | None = None

    async def async_start(self) -> None:
        """Watch the configured entities and apply the initial state."""
        watched_entities = list(dict.fromkeys([self.target_climate, *self.room_climates]))
        self._unsubscribe = async_track_state_change_event(
            self.hass, watched_entities, self._handle_state_change
        )
        await self.async_update()

    def async_stop(self) -> None:
        """Stop watching entities."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    @callback
    def _handle_state_change(self, event: Event) -> None:
        self.hass.async_create_task(self.async_update())

    async def async_update(self) -> None:
        """Update the target mode and setpoint from the room climates."""
        async with self._lock:
            room_states = [self.hass.states.get(entity_id) for entity_id in self.room_climates]
            heating_rooms = [
                state
                for state in room_states
                if state is not None and state.state == "heat"
            ]
            unavailable_rooms = [
                entity_id
                for entity_id, state in zip(self.room_climates, room_states)
                if state is None or state.state in ("unknown", "unavailable")
            ]

            if not heating_rooms:
                if unavailable_rooms:
                    _LOGGER.warning(
                        "Leaving %s unchanged because room climates are unavailable: %s",
                        self.target_climate,
                        ", ".join(unavailable_rooms),
                    )
                    return
                await self._async_set_hvac_mode("off")
                return

            await self._async_set_hvac_mode("heat")

            target_state = self.hass.states.get(self.target_climate)
            reference_temperature = self._number(
                target_state.attributes.get("current_temperature")
                if target_state is not None
                else None
            )
            if reference_temperature is None:
                _LOGGER.warning(
                    "Cannot calculate the Atlantic setpoint: %s has no valid current temperature",
                    self.target_climate,
                )
                return

            deficits = []
            for room in heating_rooms:
                current = self._number(room.attributes.get("current_temperature"))
                target = self._number(room.attributes.get("temperature"))
                if current is not None and target is not None:
                    deficits.append(target - current)

            if not deficits:
                _LOGGER.warning(
                    "Cannot calculate the Atlantic setpoint: heating room climates have no "
                    "valid current and target temperatures"
                )
                return

            largest_deficit = max(deficits)
            if largest_deficit > self.deadband:
                delta = self._delta_for_deficit(largest_deficit)
            elif largest_deficit < -self.deadband:
                delta = max(-self.delta_max, largest_deficit)
            else:
                delta = 0.0
            target_temperature = self._round_half_degree(
                min(28.0, max(15.0, reference_temperature + delta))
            )
            current_target = (
                self._number(target_state.attributes.get("temperature"))
                if target_state is not None
                else None
            )
            if current_target is None or abs(target_temperature - current_target) >= 0.5:
                try:
                    await self.hass.services.async_call(
                        "climate",
                        "set_temperature",
                        {
                            "entity_id": self.target_climate,
                            "temperature": target_temperature,
                        },
                        blocking=True,
                    )
                except HomeAssistantError:
                    _LOGGER.exception(
                        "Failed to set %s to %.1f °C",
                        self.target_climate,
                        target_temperature,
                    )

    async def _async_set_hvac_mode(self, mode: str) -> None:
        """Set the target climate mode only when it differs."""
        target_state = self.hass.states.get(self.target_climate)
        if target_state is not None and target_state.state == mode:
            return
        try:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self.target_climate, "hvac_mode": mode},
                blocking=True,
            )
        except HomeAssistantError:
            _LOGGER.exception("Failed to set %s to HVAC mode %s", self.target_climate, mode)

    def _delta_for_deficit(self, deficit: float) -> float:
        """Map a positive room deficit to a bounded heat-pump offset."""
        if deficit <= self.deadband:
            return 0.0
        if deficit < 1.0:
            fraction = (deficit - self.deadband) / (1.0 - self.deadband)
            delta = 0.5 + fraction
        elif deficit < 3.0:
            delta = 1.5 + (deficit - 1.0) * 1.25
        else:
            delta = 4.0 + (deficit - 3.0)
        return min(delta, self.delta_max)

    @staticmethod
    def _number(value: Any) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _round_half_degree(temperature: float) -> float:
        return math.floor(temperature * 2.0 + 0.5) / 2.0


__all__ = [
    "async_setup",
    "async_setup_entry",
    "async_unload_entry",
    "async_reload_entry",
]
