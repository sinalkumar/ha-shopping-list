"""UID-preserving write-through tests against Home Assistant's Todo model."""

import asyncio
import sys
from types import ModuleType, SimpleNamespace

from homeassistant.components.todo import TodoItem
from homeassistant.components.todo.const import TodoItemStatus, TodoListEntityFeature

coordinator_module = ModuleType("smart_shopping_route.coordinator")
coordinator_module.RouteCoordinator = object
sys.modules["smart_shopping_route.coordinator"] = coordinator_module

from smart_shopping_route.models import RouteSection  # noqa: E402
from smart_shopping_route.todo import SmartShoppingRouteTodo  # noqa: E402


class Source:
    entity_id = "todo.synthetic_source"
    supported_features = int(
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
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


def make_entity(*, prefix: bool = False) -> tuple[SmartShoppingRouteTodo, Coordinator]:
    coordinator = Coordinator()
    entry = SimpleNamespace(
        entry_id="synthetic-entry",
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
