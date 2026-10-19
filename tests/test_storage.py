"""Real Home Assistant storage: a queued write must not undo a removal.

``Store`` arms a final-write listener per instance when a save happens while
Home Assistant is stopping, and ``Store.async_remove`` only cancels the
listener owned by that same instance. Deleting a profile's price store through
a fresh instance therefore removed the file and then let the owner's queued
write recreate it. Every other test substitutes the store, so this file uses the
real one.
"""

import asyncio
from pathlib import Path

from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import CoreState, HomeAssistant

# Bound at import time: the shared fixture replaces the attribute on the storage
# module, and this module deliberately exercises the real class.
from smart_shopping_route.const import PRICE_STORAGE_KEY_PREFIX
from smart_shopping_route.storage import PriceStore as RealPriceStore


def price_store_path(hass: HomeAssistant, entry_id: str) -> Path:
    return Path(hass.config.path(".storage", f"{PRICE_STORAGE_KEY_PREFIX}.{entry_id}"))


def test_a_removed_price_store_is_not_recreated_by_its_final_write(tmp_path) -> None:
    async def scenario() -> None:
        hass = HomeAssistant(str(tmp_path))
        owner = RealPriceStore(hass, "review-entry")
        path = price_store_path(hass, "review-entry")

        await owner.async_save({"pins": {"milk": {"query": "Milk"}}})
        assert path.exists()

        # Shutdown is where the owner arms its final-write listener.
        hass.state = CoreState.stopping
        await owner.async_save({"pins": {"milk": {"query": "Milk"}}})

        await owner.async_remove()
        assert not path.exists()

        hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
        await hass.async_block_till_done()
        assert not path.exists(), "the owner's queued write recreated the store"

    asyncio.run(scenario())


def test_removing_the_owner_store_twice_is_harmless(tmp_path) -> None:
    async def scenario() -> None:
        hass = HomeAssistant(str(tmp_path))
        owner = RealPriceStore(hass, "review-entry")
        await owner.async_save({"pins": {}})

        await owner.async_remove()
        await owner.async_remove()
        assert not price_store_path(hass, "review-entry").exists()

    asyncio.run(scenario())
