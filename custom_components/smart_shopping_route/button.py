"""Voice-friendly route refresh button."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Availability is derived, so it must be re-evaluated when the source
        # changes. Without this the entity keeps whatever it computed during
        # setup, which is often "unavailable" because platform setup can run
        # before the source entity is resolvable.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.coordinator.signal, self._handle_update
            )
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self.coordinator.source.entity_id],
                self._handle_dependency_state,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @callback
    def _handle_dependency_state(self, _event) -> None:
        self.async_write_ha_state()

    async def async_press(self) -> None:
        await self.coordinator.async_refresh()
