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
    entities: list[SensorEntity] = [UnclassifiedSensor(entry, coordinator)]
    if coordinator.prices.enabled:
        # Only create price entities for profiles that opted in, so upgrading a
        # route-only profile does not add permanently unavailable sensors.
        entities += [
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
        return (
            self.coordinator.available
            and self.coordinator.prices.enabled
            and self.coordinator.prices.snapshot.status
            in ("current", "partial", "empty")
        )

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.prices.enabled:
            return None
        snapshot = self.coordinator.prices.snapshot
        return (
            snapshot.total
            if snapshot.status in ("current", "partial", "empty")
            else None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self.coordinator.prices.snapshot
        return {
            "chain": self.coordinator.prices.chain,
            **self.coordinator.prices.status_attributes(),
            "special_savings": snapshot.savings,
        }


class SpecialsSensor(RouteSensor):
    """Items that are cheaper now than their reference price.

    A retailer-published was-price is used when the chain provides one. For
    chains that publish none, the reference is our own previous reading, so
    "down $1.20 since last check" is the honest signal.
    """

    _attr_translation_key = "specials"
    _attr_icon = "mdi:tag-outline"
    _attr_native_unit_of_measurement = "items"

    def __init__(self, entry: ConfigEntry, coordinator: RouteCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_specials"

    @property
    def available(self) -> bool:
        return (
            self.coordinator.available
            and self.coordinator.prices.enabled
            and self.coordinator.prices.snapshot.status
            in ("current", "partial", "empty")
        )

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.prices.enabled:
            return None
        snapshot = self.coordinator.prices.snapshot
        if snapshot.status not in ("current", "partial", "empty"):
            return None
        return len(snapshot.specials) + len(snapshot.drops)

    @property
    def _promotions(self) -> list[dict[str, Any]]:
        """Items the retailer flags as promoted without giving a was-price.

        Foodstuffs publishes a promotion record but no reference price, and most
        of those promotions do not lower the shelf price, so this is reported as
        context rather than counted as savings.
        """

        snapshot = self.coordinator.prices.snapshot
        return [
            {
                "summary": key,
                "product": quote.product_name,
                "price": quote.price,
                "valid_until": quote.valid_until,
                "lowest_seen": snapshot.lowest_for(key),
                "at_lowest": snapshot.is_at_lowest(key),
            }
            for key, quote in snapshot.quotes.items()
            if key in snapshot.active
            and not snapshot.stale_for(key)
            and quote.on_promotion
            and not quote.is_special
        ]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.prices.enabled:
            return {}
        snapshot = self.coordinator.prices.snapshot
        return {
            **self.coordinator.prices.status_attributes(),
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
            "total_savings": round(
                snapshot.savings + sum(drop for _k, _q, drop in snapshot.drops), 2
            ),
            "price_drops": [
                {
                    "summary": key,
                    "product": quote.product_name,
                    "price": quote.price,
                    "previous_price": quote.price + drop,
                    "drop": drop,
                    "lowest_seen": snapshot.lowest_for(key),
                    "at_lowest": snapshot.is_at_lowest(key),
                }
                for key, quote, drop in snapshot.drops
            ],
            "promotions": self._promotions,
        }
