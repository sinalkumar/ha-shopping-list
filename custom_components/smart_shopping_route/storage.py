"""Home Assistant storage wrapper for one route profile."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .cache import ClassificationCache
from .const import STORAGE_KEY_PREFIX, STORAGE_VERSION


class CacheStore:
    """Persist a classification cache under the config entry ID."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry_id}"
        )

    async def async_load(self) -> ClassificationCache:
        return ClassificationCache.from_dict(await self._store.async_load() or {})

    async def async_save(self, cache: ClassificationCache) -> None:
        await self._store.async_save(cache.as_dict())
