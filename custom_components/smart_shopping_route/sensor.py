"""Privacy-safe route status and price sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
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
    coordinator: RouteCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        UnclassifiedSensor(entry, coordinator),
        BasketTotalSensor(entry, coordinator),
        SpecialsSensor(entry, coordinator),
    ]
    async_add_entities(entities)


class RouteSensor(SensorEntity):
    """Base for sensors that follow the coordinator."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, coordinator: RouteCoordinator) -> None:
        self.entry = entry
        self.coordinator = coordinator
        self._attr_device_info = route_device_info(entry)

    @property
    def available(self) -> bool:
        return self.coordinator.available

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.coordinator.signal, self._handle_update
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.coordinator.price_signal, self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class UnclassifiedSensor(RouteSensor):
    """Count unclassified incomplete items without exposing their text."""

    _attr_translation_key = "unclassified_items"
    _attr_icon = "mdi:help-box-multiple-outline"
    _attr_native_unit_of_measurement = "items"

    def __init__(self, entry: ConfigEntry, coordinator: RouteCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_unclassified"

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


class BasketTotalSensor(RouteSensor):
    """Estimated cost of the incomplete items that have a known price."""

    _attr_translation_key = "basket_total"
    _attr_icon = "mdi:cart-outline"
    _attr_native_unit_of_measurement = "NZD"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, coordinator: RouteCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_basket_total"

    @property
    def available(self) -> bool:
        return self.coordinator.available and self.coordinator.prices.enabled

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.prices.enabled:
            return None
        return self.coordinator.prices.snapshot.total

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self.coordinator.prices.snapshot
        return {
            "chain": self.coordinator.prices.chain,
            "priced_items": snapshot.priced_count,
            "unmatched_items": snapshot.unmatched_count,
            "special_savings": snapshot.savings,
        }


class SpecialsSensor(RouteSensor):
    """How many items on the list are currently on special."""

    _attr_translation_key = "specials"
    _attr_icon = "mdi:tag-outline"
    _attr_native_unit_of_measurement = "items"

    def __init__(self, entry: ConfigEntry, coordinator: RouteCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_specials"

    @property
    def available(self) -> bool:
        return self.coordinator.available and self.coordinator.prices.enabled

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.prices.enabled:
            return None
        return len(self.coordinator.prices.snapshot.specials)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.prices.enabled:
            return {}
        snapshot = self.coordinator.prices.snapshot
        return {
            "items": [
                {
                    "summary": key,
                    "product": quote.product_name,
                    "price": quote.price,
                    "was_price": quote.was_price,
                    "savings": quote.savings,
                    "valid_until": quote.valid_until,
                }
                for key, quote in snapshot.specials
            ],
            "total_savings": snapshot.savings,
        }
