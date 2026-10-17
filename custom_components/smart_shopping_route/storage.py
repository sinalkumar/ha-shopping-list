"""Home Assistant storage wrapper for one route profile."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .cache import ClassificationCache
from .const import DOMAIN, STORAGE_KEY_PREFIX, STORAGE_VERSION

STORES_DATA_KEY = f"{DOMAIN}_stores"


class CacheStore:
    """Persist a classification cache under the config entry ID."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry_id}"
        )

    async def async_load(self) -> ClassificationCache:
        return ClassificationCache.from_dict(await self._store.async_load() or {})

    async def async_save(self, cache: ClassificationCache) -> None:
        """Write the cache to disk."""

        await self._store.async_save(cache.as_dict())

    async def async_remove(self) -> None:
        """Delete the stored cache and cancel this instance's pending writes."""

        await self._store.async_remove()


def async_get_store(hass: HomeAssistant, entry_id: str) -> CacheStore:
    """Return the single store instance for a profile.

    One instance per profile is required, not a convenience: ``Store`` cancels
    its own final-write listener per instance, so removing the file through a
    second instance could not stop a pending write from recreating it.
    """

    stores: dict[str, CacheStore] = hass.data.setdefault(STORES_DATA_KEY, {})
    store = stores.get(entry_id)
    if store is None:
        store = stores[entry_id] = CacheStore(hass, entry_id)
    return store


@callback
def async_pop_store(hass: HomeAssistant, entry_id: str) -> CacheStore:
    """Return the profile's store for removal, or a fresh one if none exists."""

    stores: dict[str, CacheStore] = hass.data.get(STORES_DATA_KEY) or {}
    store = stores.pop(entry_id, None)
    if store is None:
        store = CacheStore(hass, entry_id)
    return store
