"""Config dependency validation tests."""

from types import SimpleNamespace

import pytest
from homeassistant.components.todo.const import TodoListEntityFeature

from smart_shopping_route import config_flow


class States:
    def __init__(self, values):
        self.values = values

    def get(self, entity_id):
        return self.values.get(entity_id)


def state(features: int):
    return SimpleNamespace(attributes={"supported_features": features})


def flow_with(source, ai_task, monkeypatch, *, source_platform="synthetic"):
    flow = object.__new__(config_flow.SmartShoppingRouteConfigFlow)
    flow.hass = SimpleNamespace(
        states=States({"todo.source": source, "ai_task.test": ai_task})
    )
    registry = SimpleNamespace(
        async_get=lambda _entity_id: SimpleNamespace(platform=source_platform)
    )
    monkeypatch.setattr(config_flow.er, "async_get", lambda _hass: registry)
    return flow


def input_data():
    return {"source_entity_id": "todo.source", "ai_task_entity_id": "ai_task.test"}


def test_valid_dependencies(monkeypatch) -> None:
    flow = flow_with(
        state(int(TodoListEntityFeature.UPDATE_TODO_ITEM)),
        state(int(config_flow.AITaskEntityFeature.GENERATE_DATA)),
        monkeypatch,
    )
    flow._validate_entities(input_data())


@pytest.mark.parametrize(
    ("source", "ai_task", "platform", "error"),
    [
        (None, state(1), "synthetic", "source_not_found"),
        (
            state(int(TodoListEntityFeature.CREATE_TODO_ITEM)),
            state(1),
            "synthetic",
            "source_no_update",
        ),
        (
            state(int(TodoListEntityFeature.UPDATE_TODO_ITEM)),
            None,
            "synthetic",
            "ai_task_not_found",
        ),
        (
            state(int(TodoListEntityFeature.UPDATE_TODO_ITEM)),
            state(0),
            "synthetic",
            "ai_task_no_generate_data",
        ),
        (
            state(int(TodoListEntityFeature.UPDATE_TODO_ITEM)),
            state(1),
            "smart_shopping_route",
            "source_is_route",
        ),
    ],
)
def test_invalid_dependency(source, ai_task, platform, error, monkeypatch) -> None:
    flow = flow_with(source, ai_task, monkeypatch, source_platform=platform)
    with pytest.raises(ValueError, match=error):
        flow._validate_entities(input_data())
