"""Voice-friendly route refresh button."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import RouteCoordinator
from .device import route_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([RefreshRouteButton(entry, hass.data[DOMAIN][entry.entry_id])])


class RefreshRouteButton(ButtonEntity):
    """Refresh the source view and enqueue unknown classifications."""

    _attr_has_entity_name = True
    _attr_translation_key = "refresh"
    _attr_icon = "mdi:refresh"

    def __init__(self, entry: ConfigEntry, coordinator: RouteCoordinator) -> None:
        self._attr_unique_id = f"{entry.entry_id}_refresh"
        self._attr_device_info = route_device_info(entry)
        self.coordinator = coordinator

    @property
    def available(self) -> bool:
        return self.coordinator.available

    async def async_press(self) -> None:
        await self.coordinator.async_refresh()
