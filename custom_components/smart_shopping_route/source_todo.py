"""Narrow adapter around a source Home Assistant Todo entity."""

from __future__ import annotations

import copy
from collections.abc import Callable

from homeassistant.components.todo import TodoItem, TodoListEntity
from homeassistant.components.todo.const import DATA_COMPONENT
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError


class SourceTodoAdapter:
    """Locate, subscribe to, and mutate a source TodoListEntity.

    Core exposes TodoListEntity.async_subscribe_updates publicly, but as of
    Home Assistant 2026.8 the only in-process lookup is the todo
    EntityComponent stored under DATA_COMPONENT. Keeping that detail here makes
    it easy to replace when Core offers a public entity lookup helper.
    """

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self._entity: TodoListEntity | None = None

    @property
    def entity(self) -> TodoListEntity:
        entity = self._resolve()
        if entity is None:
            raise HomeAssistantError("Source to-do entity is unavailable")
        return entity

    def _resolve(self) -> TodoListEntity | None:
        component = self.hass.data.get(DATA_COMPONENT)
        candidate = component.get_entity(self.entity_id) if component else None
        self._entity = candidate if isinstance(candidate, TodoListEntity) else None
        return self._entity

    @property
    def available(self) -> bool:
        entity = self._resolve()
        state = self.hass.states.get(self.entity_id)
        return (
            entity is not None
            and state is not None
            and state.state not in ("unavailable", "unknown")
        )

    @property
    def supported_features(self) -> int:
        return int(self.entity.supported_features or 0)

    def items(self) -> list[TodoItem]:
        return [copy.copy(item) for item in self.entity.todo_items or []]

    @callback
    def subscribe(self, listener: Callable[[list[TodoItem]], None]) -> CALLBACK_TYPE:
        @callback
        def _updated(items: list[TodoItem] | None) -> None:
            listener([copy.copy(item) for item in items or []])

        return self.entity.async_subscribe_updates(_updated)

    async def async_create(self, item: TodoItem) -> None:
        await self.entity.async_create_todo_item(item)

    async def async_update(self, item: TodoItem) -> None:
        await self.entity.async_update_todo_item(item)

    async def async_delete(self, uids: list[str]) -> None:
        await self.entity.async_delete_todo_items(uids)
