"""Privacy-safe route status sensor."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import RouteCoordinator
from .device import route_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([UnclassifiedSensor(entry, hass.data[DOMAIN][entry.entry_id])])


class UnclassifiedSensor(SensorEntity):
    """Count unclassified incomplete items without exposing their text."""

    _attr_has_entity_name = True
    _attr_translation_key = "unclassified_items"
    _attr_icon = "mdi:help-box-multiple-outline"
    _attr_native_unit_of_measurement = "items"

    def __init__(self, entry: ConfigEntry, coordinator: RouteCoordinator) -> None:
        self._attr_unique_id = f"{entry.entry_id}_unclassified"
        self._attr_device_info = route_device_info(entry)
        self.coordinator = coordinator

    @property
    def available(self) -> bool:
        return self.coordinator.available

    @property
    def native_value(self) -> int:
        return self.coordinator.unclassified_count

    @property
    def extra_state_attributes(self) -> dict[str, int | bool]:
        return {
            "cached_classifications": len(self.coordinator.cache.entries),
            "pending_classifications": len(self.coordinator.pending),
            "scheduled_retries": self.coordinator.retry_scheduled_count,
            "ai_available": self.coordinator.ai_available,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.coordinator.signal, self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
