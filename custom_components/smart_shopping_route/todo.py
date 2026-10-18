"""Derived, write-through Todo entity."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from hashlib import sha256

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components.todo import TodoItem, TodoListEntity
from homeassistant.components.todo.const import TodoItemStatus, TodoListEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
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
from .models import (
    decorate_summary,
    normalize_summary,
    section_names,
    sort_items,
    strip_display_prefix,
)


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
        features = int(self.coordinator.source.supported_features) & int(allowed)
        # The description is derived from prices, so this view always accepts the
        # field even when the source cannot store it. It must: the to-do dialog
        # sends every field back on save, and Home Assistant rejects the whole
        # call when a field is not advertised, which blocked renaming an item.
        if self.coordinator.prices.enabled:
            features |= int(TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM)
        return features

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
            price = self._price_description(source_item)
            if price:
                generated = f"Price info (automatic; edit notes below): {price}"
                checksum = sha256(generated.encode()).hexdigest()
                item.description = (
                    f"<!-- ssr-price:start -->\n{generated}\n"
                    f"<!-- ssr-price:sha256={checksum} -->\n"
                    f"<!-- ssr-price:end -->"
                    + (
                        f"\n\n{source_item.description}"
                        if source_item.description is not None
                        else ""
                    )
                )
            decorated.append(item)
        return decorated

    def _price_description(self, item: TodoItem) -> str | None:
        """Return a price line: price, was-price, size, unit price, product.

        "Was" is reserved for a fresh retailer reference. Recent historical
        observations are dated and labelled separately; stale quotes never
        claim a current discount.
        """

        quote = self.coordinator.prices.quote_for(item.summary or "")
        if quote is None:
            return None
        parts = [f"${quote.price:.2f}"]

        snapshot = self.coordinator.prices.snapshot
        key = normalize_summary(item.summary or "")
        if snapshot.stale_for(key):
            parts.append("STALE — refresh prices")
        elif quote.is_special and quote.was_price is not None:
            parts.append(f"was ${quote.was_price:.2f}")
        else:
            reference = snapshot.reference_point(key)
            if reference is not None and reference.price > quote.price:
                date = datetime.fromtimestamp(reference.ts, UTC).date().isoformat()
                parts.append(f"previously seen ${reference.price:.2f} ({date})")

        if quote.size:
            parts.append(quote.size)
        if quote.unit_price:
            parts.append(quote.unit_price)
        parts.append(quote.product_name)
        return " · ".join(parts)

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
            source = next(
                (raw for raw in self.coordinator.items if raw.uid == item.uid), None
            )
            known = (
                (self.coordinator.section_for(source),)
                if source is not None
                else section_names(self.coordinator.sections)
            )
            forwarded.summary = strip_display_prefix(forwarded.summary, known)
        return forwarded

    def _forward_notes(self, item: TodoItem, *, update: bool) -> TodoItem:
        """Remove only our leading price block; notes remain source-owned.

        HA sends the complete displayed item on rename/status updates. In
        particular, None means *clear notes* for local_todo, not *omit field*.
        """

        forwarded = self._forward_summary(item)
        description = forwarded.description
        source = (
            next((raw for raw in self.coordinator.items if raw.uid == item.uid), None)
            if update
            else None
        )
        # A genuine source note is never interpreted as generated metadata.
        if description != (source.description if source else None):
            if description and description.startswith("<!-- ssr-price:start -->\n"):
                block, sep, notes = description.partition("\n<!-- ssr-price:end -->")
                if not sep:
                    raise ServiceValidationError(
                        "Automatic price information was changed. Reopen the item "
                        "and edit only the notes below it."
                    )
                generated, hash_sep, checksum = block.removeprefix(
                    "<!-- ssr-price:start -->\n"
                ).rpartition("\n<!-- ssr-price:sha256=")
                if (
                    not hash_sep
                    or checksum != f"{sha256(generated.encode()).hexdigest()} -->"
                ):
                    raise ServiceValidationError(
                        "Automatic price information is read-only. Reopen the item and edit notes below it, or use product pinning."
                    )
                description = notes[2:] if notes.startswith("\n\n") else None
        if not self.coordinator.source.supported_features & int(
            TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
        ):
            if description:
                raise ServiceValidationError(
                    "The source list does not support notes. Use product pinning "
                    "to correct prices; automatic price information is read-only."
                )
            description = None
        forwarded.description = description
        return forwarded

    async def _async_check_source_control(self) -> None:
        user_id = getattr(getattr(self, "_context", None), "user_id", None)
        if user_id:
            user = await self.hass.auth.async_get_user(user_id)
            if user is None or not user.permissions.check_entity(
                self.coordinator.source.entity_id, POLICY_CONTROL
            ):
                raise ServiceValidationError(
                    "Not authorized to control the source list"
                )

    async def async_create_todo_item(self, item: TodoItem) -> None:
        await self._async_check_source_control()
        await self.coordinator.source.async_create(
            self._forward_notes(item, update=False)
        )

    async def async_update_todo_item(self, item: TodoItem) -> None:
        await self._async_check_source_control()
        await self.coordinator.source.async_update(
            self._forward_notes(item, update=True)
        )

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        await self._async_check_source_control()
        await self.coordinator.source.async_delete(uids)
