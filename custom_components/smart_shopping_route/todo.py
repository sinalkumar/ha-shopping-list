"""Derived, write-through Todo entity."""

from __future__ import annotations

import copy

from homeassistant.components.todo import TodoItem, TodoListEntity
from homeassistant.components.todo.const import TodoItemStatus, TodoListEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_AI_TASK_ENTITY_ID,
    CONF_DISPLAY_SECTION_PREFIX,
    CONF_PROFILE_NAME,
    CONF_SHOW_COMPLETED,
    DOMAIN,
)
from .coordinator import RouteCoordinator
from .device import route_device_info
from .models import decorate_summary, section_names, sort_items, strip_display_prefix


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the route Todo entity."""

    async_add_entities([
        SmartShoppingRouteTodo(entry, hass.data[DOMAIN][entry.entry_id])
    ])


class SmartShoppingRouteTodo(TodoListEntity):
    """Calculated view that delegates all mutations to the source list."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, coordinator: RouteCoordinator) -> None:
        self.entry = entry
        self.coordinator = coordinator
        self._attr_name = entry.data[CONF_PROFILE_NAME]
        self._attr_unique_id = f"{entry.entry_id}_route"
        self._attr_device_info = route_device_info(entry)
        self._attr_supported_features = self._proxy_features()

    def _proxy_features(self) -> int:
        allowed = (
            TodoListEntityFeature.CREATE_TODO_ITEM
            | TodoListEntityFeature.DELETE_TODO_ITEM
            | TodoListEntityFeature.UPDATE_TODO_ITEM
            | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
            | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
            | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
        )
        return int(self.coordinator.source.supported_features) & int(allowed)

    @property
    def _prefix_enabled(self) -> bool:
        return bool(self.entry.data.get(CONF_DISPLAY_SECTION_PREFIX, False))

    @property
    def available(self) -> bool:
        return self.coordinator.available

    @property
    def todo_items(self) -> list[TodoItem]:
        classifications = self.coordinator.classifications()
        items = sort_items(
            self.coordinator.items,
            self.coordinator.sections,
            classifications,
            show_completed=bool(self.entry.data.get(CONF_SHOW_COMPLETED, False)),
            completed_status=TodoItemStatus.COMPLETED,
        )
        decorated: list[TodoItem] = []
        for source_item in items:
            item = copy.copy(source_item)
            if item.summary and self._prefix_enabled:
                item.summary = decorate_summary(
                    item.summary,
                    self.coordinator.section_for(source_item),
                    True,
                )
            decorated.append(item)
        return decorated

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.coordinator.signal, self._handle_update
            )
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [
                    self.coordinator.source.entity_id,
                    self.entry.data[CONF_AI_TASK_ENTITY_ID],
                ],
                self._handle_dependency_state,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @callback
    def _handle_dependency_state(self, _event) -> None:
        self.async_write_ha_state()

    def _forward_summary(self, item: TodoItem) -> TodoItem:
        forwarded = copy.copy(item)
        if forwarded.summary and self._prefix_enabled:
            forwarded.summary = strip_display_prefix(
                forwarded.summary, section_names(self.coordinator.sections)
            )
        return forwarded

    async def async_create_todo_item(self, item: TodoItem) -> None:
        await self.coordinator.source.async_create(self._forward_summary(item))

    async def async_update_todo_item(self, item: TodoItem) -> None:
        await self.coordinator.source.async_update(self._forward_summary(item))

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        await self.coordinator.source.async_delete(uids)
