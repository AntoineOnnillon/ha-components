"""Diagnostic entities for the Cumulus PV controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import _diagnostics_signal
from .const import DOMAIN

if TYPE_CHECKING:
    from . import CumulusPVController


SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="mode",
        translation_key="mode",
    ),
    SensorEntityDescription(
        key="requested_duty",
        translation_key="requested_duty",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="pv_surplus",
        translation_key="pv_surplus",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="pv_budget",
        translation_key="pv_budget",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="pv_surplus_used_today",
        translation_key="pv_surplus_used_today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="effective_target",
        translation_key="effective_target",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="horizon_available",
        translation_key="horizon_available",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="energy_deficit_forecast",
        translation_key="energy_deficit_forecast",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="peak_window_active",
        translation_key="peak_window_active",
        state_class=SensorStateClass.MEASUREMENT,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the controller diagnostic sensors."""
    controller: CumulusPVController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        CumulusPVDiagnosticSensor(controller, entry, description)
        for description in SENSOR_DESCRIPTIONS
    )


class CumulusPVDiagnosticSensor(SensorEntity):
    """Expose one diagnostic value from the PV controller."""

    entity_description: SensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        controller: CumulusPVController,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        self.controller = controller
        self.entry = entry
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Cumulus PV Controller",
            manufacturer="Home Assistant",
        )

    @property
    def native_value(self) -> float | int | str | None:
        """Return the controller diagnostic."""
        return self.controller.diagnostics[self.entity_description.key]

    async def async_added_to_hass(self) -> None:
        """Subscribe to controller diagnostic changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                _diagnostics_signal(self.entry.entry_id),
                self._handle_diagnostics_update,
            )
        )

    @callback
    def _handle_diagnostics_update(self) -> None:
        """Write updated state."""
        self.async_write_ha_state()
