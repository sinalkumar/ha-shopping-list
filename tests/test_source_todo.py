"""The source subscription must follow the entity ID across a reload.

A to-do integration can unload and re-create its entity while keeping the same
entity ID. The replacement is a different object with its own listener list, so
a subscription taken once at setup kept reporting the removed entity's items
while the route still looked available.
"""

from types import SimpleNamespace

import pytest
from homeassistant.components.todo import TodoItem, TodoListEntity
from homeassistant.components.todo.const import DATA_COMPONENT
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event

from smart_shopping_route.source_todo import SourceTodoAdapter

ENTITY = "todo.synthetic_source"


class FakeEntity(TodoListEntity):
    """A real TodoListEntity, so subscription semantics are the shipped ones."""

    def __init__(self, summaries: list[str]) -> None:
        self._attr_todo_items = [
            TodoItem(uid=f"uid-{index}", summary=summary)
            for index, summary in enumerate(summaries)
        ]


class FakeBus:
    """Minimal event bus with the filter contract the tracker relies on."""

    def __init__(self) -> None:
        self.listeners: list[tuple[str, object, object]] = []

    def async_listen(self, event_type, callback, event_filter=None):
        entry = (event_type, callback, event_filter)
        self.listeners.append(entry)

        def remove() -> None:
            if entry in self.listeners:
                self.listeners.remove(entry)

        return remove

    def fire(self, entity_id: str, state: str = "1") -> None:
        data = {
            "entity_id": entity_id,
            "old_state": None,
            "new_state": SimpleNamespace(state=state),
        }
        for event_type, callback, event_filter in list(self.listeners):
            if event_type != EVENT_STATE_CHANGED:
                continue
            if event_filter is not None and not event_filter(data):
                continue
            callback(Event(event_type, data))


@pytest.fixture
def hass():
    """A hass stand-in that can swap the entity behind one entity ID."""

    current: dict[str, TodoListEntity | None] = {"entity": None}
    bus = FakeBus()
    stand_in = SimpleNamespace(
        data={
            DATA_COMPONENT: SimpleNamespace(
                get_entity=lambda _entity_id: current["entity"]
            )
        },
        bus=bus,
        states=SimpleNamespace(get=lambda _entity_id: SimpleNamespace(state="1")),
        async_run_hass_job=lambda job, event: job.target(event),
    )
    stand_in.current = current
    return stand_in


def summaries(seen: list[list[TodoItem]]) -> list[str]:
    return [item.summary for item in seen[-1]]


def test_a_replaced_source_entity_is_followed_and_replayed(hass) -> None:
    original = FakeEntity(["Before reload"])
    hass.current["entity"] = original
    adapter = SourceTodoAdapter(hass, ENTITY)
    seen: list[list[TodoItem]] = []
    unsubscribe = adapter.subscribe(seen.append)

    replacement = FakeEntity(["After reload"])
    hass.current["entity"] = replacement
    hass.bus.fire(ENTITY)

    assert summaries(seen) == ["After reload"]
    # The removed entity must not be able to speak for the route any more.
    original.async_update_listeners()
    assert len(seen) == 1
    replacement.async_update_listeners()
    assert summaries(seen) == ["After reload"]
    assert len(seen) == 2
    unsubscribe()


def test_state_changes_on_the_same_entity_do_not_stack_listeners(hass) -> None:
    entity = FakeEntity(["Milk"])
    hass.current["entity"] = entity
    adapter = SourceTodoAdapter(hass, ENTITY)
    seen: list[list[TodoItem]] = []
    unsubscribe = adapter.subscribe(seen.append)

    for _ in range(3):
        hass.bus.fire(ENTITY)

    entity.async_update_listeners()
    assert len(seen) == 1
    unsubscribe()


def test_a_different_entity_state_change_is_ignored(hass) -> None:
    entity = FakeEntity(["Milk"])
    hass.current["entity"] = entity
    adapter = SourceTodoAdapter(hass, ENTITY)
    seen: list[list[TodoItem]] = []
    unsubscribe = adapter.subscribe(seen.append)

    hass.bus.fire("todo.somewhere_else")
    assert seen == []
    unsubscribe()


def test_an_entity_that_appears_after_setup_is_picked_up(hass) -> None:
    """Setup can run before the source is in the entity component."""

    adapter = SourceTodoAdapter(hass, ENTITY)
    seen: list[list[TodoItem]] = []
    unsubscribe = adapter.subscribe(seen.append)

    assert adapter.available is False
    hass.current["entity"] = FakeEntity(["Milk"])
    hass.bus.fire(ENTITY)

    assert summaries(seen) == ["Milk"]
    assert adapter.available is True
    unsubscribe()


def test_unsubscribe_stops_the_entity_and_state_listeners(hass) -> None:
    entity = FakeEntity(["Milk"])
    hass.current["entity"] = entity
    adapter = SourceTodoAdapter(hass, ENTITY)
    seen: list[list[TodoItem]] = []
    unsubscribe = adapter.subscribe(seen.append)

    unsubscribe()

    assert hass.bus.listeners == []
    entity.async_update_listeners()
    assert seen == []
