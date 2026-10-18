"""Push-driven route coordinator and classification lifecycle."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import Any

from homeassistant.components.todo import TodoItem
from homeassistant.components.todo.const import TodoItemStatus
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .cache import ClassificationCache
from .classifier import AITaskClassifier
from .const import (
    AI_CONCURRENCY,
    AI_MIN_CONFIDENCE,
    AI_RETRY_BASE_SECONDS,
    AI_RETRY_MAX_SECONDS,
    CONF_AI_TASK_ENTITY_ID,
    CONF_SECTIONS,
    CONF_SOURCE_ENTITY_ID,
    DOMAIN,
    FALLBACK_SECTION,
    STORAGE_SAVE_DELAY,
)
from .models import RouteSection, normalize_summary, section_names
from .price_coordinator import PriceCoordinator
from .source_todo import SourceTodoAdapter
from .storage import async_get_store

_LOGGER = logging.getLogger(__name__)


class RouteCoordinator:
    """Maintain an immediate local view and classify unknown summaries."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.sections = tuple(
            RouteSection.from_mapping(raw) for raw in entry.data[CONF_SECTIONS]
        )
        self.source = SourceTodoAdapter(hass, entry.data[CONF_SOURCE_ENTITY_ID])
        self.classifier = AITaskClassifier(hass, entry.data[CONF_AI_TASK_ENTITY_ID])
        self.store = async_get_store(hass, entry.entry_id)
        self.prices = PriceCoordinator(hass, entry, summaries=self._priced_summaries)
        self.cache = ClassificationCache()
        self.items: list[TodoItem] = []
        self.pending: set[str] = set()
        self._unsubscribe: CALLBACK_TYPE | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._semaphore = asyncio.Semaphore(AI_CONCURRENCY)
        self._generation = 0
        self._attempts: dict[str, int] = {}
        self._retry_after: dict[str, float] = {}
        self._save_handle: asyncio.TimerHandle | None = None
        self._save_tasks: set[asyncio.Task[Any]] = set()
        self._price_task: asyncio.Task[Any] | None = None
        self._unsub_final_write: CALLBACK_TYPE | None = None
        self._closed = False

    @property
    def signal(self) -> str:
        return f"{DOMAIN}_{self.entry.entry_id}_update"

    @property
    def price_signal(self) -> str:
        return f"{DOMAIN}_{self.entry.entry_id}_price_update"

    def _priced_summaries(self) -> list[str]:
        """Summaries that should carry a price: incomplete items only."""

        return [
            item.summary
            for item in self.items
            if item.summary and item.status != TodoItemStatus.COMPLETED
        ]

    @property
    def available(self) -> bool:
        """The route only depends on the source list.

        The AI Task entity is unknown until its first generation, so it must
        never gate availability: a fresh profile with an empty list would
        otherwise be unusable.
        """
        return self.source.available

    @property
    def ai_available(self) -> bool:
        """Report AI Task health without blocking the route."""

        state = self.hass.states.get(self.entry.data[CONF_AI_TASK_ENTITY_ID])
        return state is not None and state.state != "unavailable"

    @property
    def retry_scheduled_count(self) -> int:
        return len(self._retry_after)

    async def async_setup(self) -> None:
        self.cache = await self.store.async_load()
        self.cache.invalidate_sections(set(section_names(self.sections)))
        self.items = self.source.items()
        self.cache.prune_uids({item.uid for item in self.items if item.uid})
        self._unsubscribe = self.source.subscribe(self._source_updated)
        # Home Assistant stops without unloading entries, so a pending debounced
        # write needs its own final-write flush or the last classification is lost.
        self._unsub_final_write = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_FINAL_WRITE, self._async_final_write
        )
        self.prices.async_setup()
        self._notify()
        self.async_classify_unknown()
        self._request_prices()

    async def async_shutdown(self) -> None:
        self._closed = True
        self._generation += 1
        self._cancel_scheduled_save()
        if self._unsub_final_write is not None:
            self._unsub_final_write()
            self._unsub_final_write = None
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        # Let any debounced write finish before the final save so that a caller
        # removing the entry can delete the file knowing no write follows.
        if self._save_tasks:
            await asyncio.gather(*self._save_tasks, return_exceptions=True)
        await self.prices.async_shutdown()
        await self.store.async_save(self.cache)

    @callback
    def _cancel_scheduled_save(self) -> None:
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None

    @callback
    def _raise_if_closed(self) -> None:
        """Refuse mutations once the profile has been unloaded or removed."""

        if self._closed:
            raise HomeAssistantError("This shopping route is no longer loaded")

    @callback
    def _schedule_save(self) -> None:
        """Coalesce background cache writes into one delayed disk write.

        The timer is owned here rather than by ``Store.async_delay_save`` so
        shutdown and profile removal can cancel it; otherwise a pending write
        could recreate the cache file after the entry was removed.
        """

        if self._closed or self._save_handle is not None:
            return
        if self.hass.state is CoreState.stopping:
            # Storage is about to be finalized; write now instead of debouncing.
            self._save_handle = self.hass.loop.call_soon(self._async_flush_save)
            return
        self._save_handle = self.hass.loop.call_later(
            STORAGE_SAVE_DELAY, self._async_flush_save
        )

    @callback
    def _async_flush_save(self) -> None:
        self._save_handle = None
        if self._closed:
            return
        task = self.entry.async_create_background_task(
            self.hass,
            self.store.async_save(self.cache),
            f"save shopping route cache for {self.entry.entry_id}",
        )
        self._save_tasks.add(task)
        task.add_done_callback(self._save_tasks.discard)

    async def _async_final_write(self, _event: Event) -> None:
        """Flush a pending debounced write during Home Assistant shutdown."""

        self._unsub_final_write = None
        self._cancel_scheduled_save()
        if self._save_tasks:
            await asyncio.gather(*self._save_tasks, return_exceptions=True)
        await self.store.async_save(self.cache)

    @callback
    def _source_updated(self, items: list[TodoItem]) -> None:
        self.items = items
        if items and self.cache.prune_uids({item.uid for item in items if item.uid}):
            self._schedule_save()
        self._notify()
        self.async_classify_unknown()
        self._request_prices()

    @callback
    def _request_prices(self) -> None:
        """Price newly seen items without blocking the route update."""

        if not self.prices.enabled or self._closed or self._price_task is not None:
            return
        task = self.entry.async_create_background_task(
            self.hass,
            self._async_price_then_notify(),
            f"price shopping route items for {self.entry.entry_id}",
        )
        self._tasks.add(task)
        self._price_task = task
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._clear_price_task)

    @callback
    def _clear_price_task(self, _task: asyncio.Task[Any]) -> None:
        self._price_task = None

    async def _async_price_then_notify(self) -> None:
        await self.prices.async_refresh(force=False)
        self._notify_prices()

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, self.signal)

    @callback
    def _notify_prices(self) -> None:
        async_dispatcher_send(self.hass, self.price_signal)

    async def async_price_refresh(self) -> None:
        """Re-price the list, keeping already known quotes."""

        if not self.prices.enabled:
            return
        await self.prices.async_refresh()
        self._notify_prices()

    def section_for(self, item: TodoItem) -> str:
        return self.cache.get(item.summary or "", item.uid) or FALLBACK_SECTION

    def classifications(self) -> dict[str, str]:
        by_summary = {
            normalize_summary(item.summary or ""): self.section_for(item)
            for item in self.items
        }
        by_uid = {
            f"uid:{item.uid}": self.section_for(item) for item in self.items if item.uid
        }
        return by_summary | by_uid

    @callback
    def async_classify_unknown(self) -> None:
        if self._closed:
            return
        now = time.monotonic()
        summaries: dict[str, str] = {}
        for item in self.items:
            if item.status == TodoItemStatus.COMPLETED or not item.summary:
                continue
            key = normalize_summary(item.summary)
            if not key or key in self.pending:
                continue
            if self.cache.get(item.summary, item.uid):
                continue
            if now < self._retry_after.get(key, 0.0):
                continue
            summaries.setdefault(key, item.summary)
        for key, summary in summaries.items():
            self.pending.add(key)
            task = self.entry.async_create_background_task(
                self.hass,
                self._async_classify_one(key, summary, self._generation),
                f"classify shopping route item for {self.entry.entry_id}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        if summaries:
            self._notify()

    async def _async_classify_one(
        self, key: str, summary: str, generation: int
    ) -> None:
        try:
            async with self._semaphore:
                result = await self.classifier.async_classify(summary, self.sections)
            if generation != self._generation or key not in {
                normalize_summary(item.summary or "") for item in self.items
            }:
                return
            if result is None:
                self._schedule_retry(key)
                return
            section, confidence = result
            if section not in section_names(self.sections):
                self._schedule_retry(key)
                return
            if confidence < AI_MIN_CONFIDENCE:
                section = FALLBACK_SECTION
            self._attempts.pop(key, None)
            self._retry_after.pop(key, None)
            # Cache a definitive "no section fits" answer too, otherwise an
            # unclassifiable item is re-sent to the provider on every change.
            self.cache.set(summary, section)
            self._schedule_save()
        finally:
            self.pending.discard(key)
            self._notify()

    def _schedule_retry(self, key: str) -> None:
        attempts = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempts
        delay = min(AI_RETRY_MAX_SECONDS, AI_RETRY_BASE_SECONDS * 2 ** (attempts - 1))
        self._retry_after[key] = time.monotonic() + delay
        # Counts only: item summaries must never reach the log.
        _LOGGER.debug(
            "Scheduling classification retry %s in %s seconds", attempts, delay
        )

    def _reset_retries(self) -> None:
        self._attempts.clear()
        self._retry_after.clear()

    async def async_refresh(self) -> None:
        if self._closed:
            return
        self.items = self.source.items()
        self._reset_retries()
        self._notify()
        self.async_classify_unknown()
        await self.async_price_refresh()

    async def async_set_section(self, uid: str, section: str, remember: bool) -> None:
        self._raise_if_closed()
        item = self.item_by_uid(uid)
        summary = (item.summary or "").strip()
        if remember:
            if not summary:
                raise ValueError("Item has no summary to remember")
            # A remembered correction must win over an earlier UID-only one.
            self.cache.forget_uid(uid)
            self.cache.set(summary, section, manual=True)
        else:
            self.cache.set_uid(uid, section)
        self._reset_retries()
        await self.store.async_save(self.cache)
        self._notify()

    async def async_forget(self, uid: str) -> None:
        self._raise_if_closed()
        item = self.item_by_uid(uid)
        self.cache.forget(item.summary or "", uid)
        self._reset_retries()
        await self.store.async_save(self.cache)
        self._notify()
        self.async_classify_unknown()

    async def async_reclassify_all(self) -> None:
        self._raise_if_closed()
        self.cache.clear_ai()
        self._reset_retries()
        await self.store.async_save(self.cache)
        self._notify()
        self.async_classify_unknown()

    async def async_clear_cache(self, include_manual: bool) -> None:
        self._raise_if_closed()
        self.cache.clear(include_manual=include_manual)
        self._reset_retries()
        await self.store.async_save(self.cache)
        self._notify()

    def item_by_uid(self, uid: str) -> TodoItem:
        for item in self.items:
            if item.uid == uid:
                return copy.copy(item)
        raise ValueError("Unknown source item UID")

    @property
    def unclassified_count(self) -> int:
        return sum(
            item.status != TodoItemStatus.COMPLETED
            and self.section_for(item) == FALLBACK_SECTION
            for item in self.items
        )
