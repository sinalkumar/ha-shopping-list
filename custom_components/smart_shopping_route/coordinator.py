"""Push-driven route coordinator and classification lifecycle."""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

from homeassistant.components.todo import TodoItem
from homeassistant.components.todo.const import TodoItemStatus
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .cache import ClassificationCache
from .classifier import AITaskClassifier
from .const import (
    AI_CONCURRENCY,
    CONF_AI_TASK_ENTITY_ID,
    CONF_SECTIONS,
    CONF_SOURCE_ENTITY_ID,
    DOMAIN,
    FALLBACK_SECTION,
)
from .models import RouteSection, normalize_summary, section_names
from .source_todo import SourceTodoAdapter
from .storage import CacheStore

_LOGGER = logging.getLogger(__name__)


class RouteCoordinator:
    """Maintain an immediate local view and classify unknown summaries."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.sections = tuple(RouteSection(**raw) for raw in entry.data[CONF_SECTIONS])
        self.source = SourceTodoAdapter(hass, entry.data[CONF_SOURCE_ENTITY_ID])
        self.classifier = AITaskClassifier(hass, entry.data[CONF_AI_TASK_ENTITY_ID])
        self.store = CacheStore(hass, entry.entry_id)
        self.cache = ClassificationCache()
        self.items: list[TodoItem] = []
        self.pending: set[str] = set()
        self.uid_sections: dict[str, str] = {}
        self.last_success: str | None = None
        self._unsubscribe: CALLBACK_TYPE | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._semaphore = asyncio.Semaphore(AI_CONCURRENCY)
        self._generation = 0

    @property
    def signal(self) -> str:
        return f"{DOMAIN}_{self.entry.entry_id}_update"

    @property
    def available(self) -> bool:
        ai_state = self.hass.states.get(self.entry.data[CONF_AI_TASK_ENTITY_ID])
        return (
            self.source.available
            and ai_state is not None
            and ai_state.state not in ("unavailable", "unknown")
        )

    async def async_setup(self) -> None:
        self.cache = await self.store.async_load()
        self.cache.invalidate_sections({name for name in section_names(self.sections)})
        self.items = self.source.items()
        self._unsubscribe = self.source.subscribe(self._source_updated)
        self._notify()
        self.async_classify_unknown()

    async def async_shutdown(self) -> None:
        self._generation += 1
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.store.async_save(self.cache)

    @callback
    def _source_updated(self, items: list[TodoItem]) -> None:
        self.items = items
        valid_uids = {item.uid for item in items if item.uid}
        self.uid_sections = {
            uid: section
            for uid, section in self.uid_sections.items()
            if uid in valid_uids
        }
        self._notify()
        self.async_classify_unknown()

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, self.signal)

    def section_for(self, item: TodoItem) -> str:
        if item.uid and item.uid in self.uid_sections:
            return self.uid_sections[item.uid]
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
        summaries: dict[str, str] = {}
        for item in self.items:
            if item.status == TodoItemStatus.COMPLETED or not item.summary:
                continue
            key = normalize_summary(item.summary)
            if (
                key
                and not self.cache.get(item.summary, item.uid)
                and key not in self.pending
            ):
                summaries[key] = item.summary
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
            if result is not None:
                section, _confidence = result
                if (
                    section in section_names(self.sections)
                    and section != FALLBACK_SECTION
                ):
                    self.cache.set(summary, section)
                    await self.store.async_save(self.cache)
        finally:
            self.pending.discard(key)
            self._notify()

    async def async_refresh(self) -> None:
        self.items = self.source.items()
        self._notify()
        self.async_classify_unknown()

    async def async_set_section(self, uid: str, section: str, remember: bool) -> None:
        item = self.item_by_uid(uid)
        if remember:
            self.cache.set(item.summary or "", section, manual=True)
        else:
            self.uid_sections[uid] = section
            self.cache.set_uid(uid, section)
        await self.store.async_save(self.cache)
        self._notify()

    async def async_forget(self, uid: str) -> None:
        item = self.item_by_uid(uid)
        self.uid_sections.pop(uid, None)
        self.cache.forget(item.summary or "", uid)
        await self.store.async_save(self.cache)
        self._notify()
        self.async_classify_unknown()

    async def async_reclassify_all(self) -> None:
        self.cache.clear_ai()
        await self.store.async_save(self.cache)
        self._notify()
        self.async_classify_unknown()

    async def async_clear_cache(self, include_manual: bool) -> None:
        self.cache.clear(include_manual=include_manual)
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
