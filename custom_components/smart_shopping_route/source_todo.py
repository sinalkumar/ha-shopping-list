"""Narrow adapter around a source Home Assistant Todo entity."""

from __future__ import annotations

import copy
from collections.abc import Callable

from homeassistant.components.todo import TodoItem, TodoListEntity
from homeassistant.components.todo.const import DATA_COMPONENT
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event


class SourceTodoAdapter:
    """Locate, subscribe to, and mutate a source TodoListEntity.

    Core exposes TodoListEntity.async_subscribe_updates publicly, but as of
    Home Assistant 2026.8 the only in-process lookup is the todo
    EntityComponent stored under DATA_COMPONENT. Keeping that detail here makes
    it easy to replace when Core offers a public entity lookup helper.

    A to-do integration can unload and re-create its entity while keeping the
    same entity ID, and the replacement is a different object with its own
    listener list. The subscription therefore follows the entity ID: it moves to
    the replacement and replays that entity's items immediately, because the
    removed entity will never report anything again. Without this the route
    kept showing the old entity's items while still reporting itself available.
    """

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self._entity: TodoListEntity | None = None
        self._listener: Callable[[list[TodoItem]], None] | None = None
        self._subscribed: TodoListEntity | None = None
        self._unsubscribe_entity: CALLBACK_TYPE | None = None
        self._unsubscribe_state: CALLBACK_TYPE | None = None

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
        """Return source features, or 0 while the entity is not resolvable."""

        entity = self._resolve()
        return int(entity.supported_features or 0) if entity else 0

    def items(self) -> list[TodoItem]:
        return [copy.copy(item) for item in self.entity.todo_items or []]

    @callback
    def subscribe(self, listener: Callable[[list[TodoItem]], None]) -> CALLBACK_TYPE:
        self._listener = listener
        self._bind(self._resolve())
        self._unsubscribe_state = async_track_state_change_event(
            self.hass, [self.entity_id], self._async_source_changed
        )

        @callback
        def unsubscribe() -> None:
            self._listener = None
            self._unbind()
            if self._unsubscribe_state is not None:
                self._unsubscribe_state()
                self._unsubscribe_state = None

        return unsubscribe

    @callback
    def _bind(self, entity: TodoListEntity | None) -> None:
        self._unbind()
        if entity is None:
            return
        self._subscribed = entity
        self._unsubscribe_entity = entity.async_subscribe_updates(self._forward)

    @callback
    def _unbind(self) -> None:
        self._subscribed = None
        if self._unsubscribe_entity is not None:
            self._unsubscribe_entity()
            self._unsubscribe_entity = None

    @callback
    def _forward(self, items: list[TodoItem] | None) -> None:
        listener = self._listener
        if listener is not None:
            listener([copy.copy(item) for item in items or []])

    @callback
    def _async_source_changed(self, _event: Event) -> None:
        """Rebind when the entity behind this entity ID has been replaced."""

        entity = self._resolve()
        if entity is None or entity is self._subscribed:
            return
        self._bind(entity)
        self._forward(entity.todo_items)

    async def async_create(self, item: TodoItem) -> None:
        await self.entity.async_create_todo_item(item)

    async def async_update(self, item: TodoItem) -> None:
        await self.entity.async_update_todo_item(item)

    async def async_delete(self, uids: list[str]) -> None:
        await self.entity.async_delete_todo_items(uids)
