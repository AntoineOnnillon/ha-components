"""Information sensors for the Atlantic heat-pump controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import _diagnostics_signal
from .const import DOMAIN

if TYPE_CHECKING:
    from . import AtlanticHeatPumpController


SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="reference_temperature",
        translation_key="reference_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="largest_room_difference",
        translation_key="largest_room_difference",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="applied_delta",
        translation_key="applied_delta",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="calculated_target",
        translation_key="calculated_target",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="heating_room_count",
        translation_key="heating_room_count",
        state_class=SensorStateClass.MEASUREMENT,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up controller diagnostic sensors."""
    controller: AtlanticHeatPumpController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        AtlanticHeatPumpDiagnosticSensor(controller, entry, description)
        for description in SENSOR_DESCRIPTIONS
    )


class AtlanticHeatPumpDiagnosticSensor(SensorEntity):
    """Expose one controller information value."""

    entity_description: SensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        controller: AtlanticHeatPumpController,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        self.controller = controller
        self.entry = entry
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Atlantic Heat Pump Controller",
            manufacturer="Atlantic",
        )

    @property
    def native_value(self) -> float | int | None:
        """Return the current diagnostic value."""
        return self.controller.diagnostics[self.entity_description.key]

    async def async_added_to_hass(self) -> None:
        """Subscribe to controller diagnostic updates."""
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
        """Write the sensor state when controller diagnostics change."""
        self.async_write_ha_state()
