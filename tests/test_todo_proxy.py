"""Route Todo entity: prefix gating, feature masking, and device grouping."""

import asyncio
from types import SimpleNamespace

from homeassistant.components.todo import TodoItem
from homeassistant.components.todo.const import TodoItemStatus, TodoListEntityFeature

import smart_shopping_route.todo as todo_module
from smart_shopping_route.models import RouteSection
from smart_shopping_route.todo import SmartShoppingRouteTodo


class Source:
    entity_id = "todo.synthetic_source"
    supported_features = int(
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.MOVE_TODO_ITEM
    )

    def __init__(self) -> None:
        self.created: list[TodoItem] = []
        self.updated: list[TodoItem] = []
        self.deleted: list[list[str]] = []

    async def async_create(self, item: TodoItem) -> None:
        self.created.append(item)

    async def async_update(self, item: TodoItem) -> None:
        self.updated.append(item)

    async def async_delete(self, uids: list[str]) -> None:
        self.deleted.append(uids)


class Coordinator:
    available = True
    sections = (RouteSection("Produce"), RouteSection("Dairy"))

    def __init__(self) -> None:
        self.source = Source()
        self.items = [
            TodoItem(uid="uid-1", summary="Milk", status=TodoItemStatus.NEEDS_ACTION),
            TodoItem(uid="uid-2", summary="Milk", status=TodoItemStatus.NEEDS_ACTION),
        ]

    def classifications(self) -> dict[str, str]:
        return {"uid:uid-1": "Dairy", "uid:uid-2": "Produce", "milk": "Dairy"}

    def section_for(self, item: TodoItem) -> str:
        return "Produce" if item.uid == "uid-2" else "Dairy"


def make_entity(*, prefix: bool = False):
    coordinator = Coordinator()
    entry = SimpleNamespace(
        entry_id="synthetic-entry",
        title="Example Supermarket",
        data={
            "profile_name": "Example Supermarket",
            "show_completed": False,
            "display_section_prefix": prefix,
            "ai_task_entity_id": "ai_task.synthetic",
        },
    )
    return SmartShoppingRouteTodo(entry, coordinator), coordinator


def test_duplicate_summaries_keep_uids_and_uid_specific_order() -> None:
    entity, _coordinator = make_entity()
    assert [(item.uid, item.summary) for item in entity.todo_items] == [
        ("uid-2", "Milk"),
        ("uid-1", "Milk"),
    ]


def test_update_delete_and_create_forward_exact_source_values() -> None:
    entity, coordinator = make_entity(prefix=True)
    assert [item.summary for item in entity.todo_items] == [
        "Produce · Milk",
        "Dairy · Milk",
    ]
    asyncio.run(
        entity.async_update_todo_item(
            TodoItem(
                uid="uid-2",
                summary="Produce · Oat milk",
                status=TodoItemStatus.COMPLETED,
            )
        )
    )
    asyncio.run(entity.async_delete_todo_items(["uid-2"]))
    asyncio.run(
        entity.async_create_todo_item(
            TodoItem(summary="Dairy · Cheese", status=TodoItemStatus.NEEDS_ACTION)
        )
    )
    assert coordinator.source.updated[0].uid == "uid-2"
    assert coordinator.source.updated[0].summary == "Oat milk"
    assert coordinator.source.deleted == [["uid-2"]]
    assert coordinator.source.created[0].uid is None
    assert coordinator.source.created[0].summary == "Cheese"


def test_prefix_is_never_stripped_when_the_feature_is_disabled() -> None:
    """With prefixes off the summary is user data, not integration decoration."""

    entity, coordinator = make_entity(prefix=False)
    asyncio.run(
        entity.async_update_todo_item(
            TodoItem(
                uid="uid-1",
                summary="Dairy · Milk",
                status=TodoItemStatus.NEEDS_ACTION,
            )
        )
    )
    assert coordinator.source.updated[0].summary == "Dairy · Milk"


def test_prefix_display_is_opt_in() -> None:
    entity, _coordinator = make_entity(prefix=False)
    assert [item.summary for item in entity.todo_items] == ["Milk", "Milk"]


def test_manual_move_is_not_proxied() -> None:
    entity, _coordinator = make_entity()
    features = TodoListEntityFeature(entity.supported_features)
    assert not features & TodoListEntityFeature.MOVE_TODO_ITEM
    assert features & TodoListEntityFeature.UPDATE_TODO_ITEM


def test_unknown_future_source_features_are_masked(monkeypatch) -> None:
    entity, coordinator = make_entity()
    coordinator.source.supported_features = int(
        TodoListEntityFeature.UPDATE_TODO_ITEM
    ) | (1 << 12)
    assert entity._proxy_features() == int(TodoListEntityFeature.UPDATE_TODO_ITEM)


def test_entity_is_grouped_under_one_profile_device() -> None:
    entity, _coordinator = make_entity()
    info = entity.device_info
    assert info["identifiers"] == {("smart_shopping_route", "synthetic-entry")}
    assert info["name"] == "Example Supermarket"
    assert info["entry_type"] == "service"


def test_entity_availability_follows_the_source_only(monkeypatch) -> None:
    entity, coordinator = make_entity()
    coordinator.available = True
    assert entity.available is True
    coordinator.available = False
    assert entity.available is False


def test_entity_uses_coordinator_sections(monkeypatch) -> None:
    entity, _coordinator = make_entity(prefix=True)
    seen = {}
    original = todo_module.strip_display_prefix

    def spy(summary, known_sections):
        seen["sections"] = tuple(known_sections)
        return original(summary, known_sections)

    monkeypatch.setattr(todo_module, "strip_display_prefix", spy)
    asyncio.run(
        entity.async_update_todo_item(
            TodoItem(
                uid="uid-1", summary="Dairy · Milk", status=TodoItemStatus.NEEDS_ACTION
            )
        )
    )
    assert seen["sections"] == ("Produce", "Dairy", "Unclassified / Other")
