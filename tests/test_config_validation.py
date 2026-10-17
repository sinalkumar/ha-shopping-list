"""Config dependency validation and profile-name handling."""

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.components.todo.const import TodoListEntityFeature

import smart_shopping_route.config_flow as config_flow


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


def other_entry(entry_id: str, profile_name: str):
    return SimpleNamespace(
        entry_id=entry_id, title=profile_name, data={"profile_name": profile_name}
    )


class FakeFlow(config_flow.SmartShoppingRouteConfigFlow):
    """Drive one profile step with stubbed Home Assistant plumbing."""

    def __init__(self, hass, *, existing_entry=None, current_entries=()):
        self.hass = hass
        self._existing_entry = existing_entry
        self._current_entries = list(current_entries)
        self.created: dict | None = None
        self.updated: dict | None = None

    def _get_reconfigure_entry(self):
        return self._existing_entry

    def _async_current_entries(self, include_ignore=None):
        return list(self._current_entries)

    def async_create_entry(self, *, title, data):
        self.created = {"title": title, "data": data}
        return self.created

    def async_update_reload_and_abort(self, entry, **kwargs):
        self.updated = kwargs
        return kwargs

    def async_show_form(self, *, step_id, data_schema, errors):
        return {"type": "form", "step_id": step_id, "errors": errors}


def make_flow(monkeypatch, **kwargs):
    hass = SimpleNamespace(
        states=States({
            "todo.source": state(int(TodoListEntityFeature.UPDATE_TODO_ITEM)),
            "ai_task.test": state(int(config_flow.AITaskEntityFeature.GENERATE_DATA)),
        })
    )
    flow = FakeFlow(hass, **kwargs)
    monkeypatch.setattr(
        config_flow.er,
        "async_get",
        lambda _hass: SimpleNamespace(
            async_get=lambda _entity_id: SimpleNamespace(platform="synthetic")
        ),
    )
    return flow


def profile_input(**overrides):
    data = {
        "profile_name": "Example Supermarket",
        "source_entity_id": "todo.source",
        "ai_task_entity_id": "ai_task.test",
        "sections": "Produce | fruit\nDairy | milk",
        "show_completed": False,
        "display_section_prefix": False,
    }
    data.update(overrides)
    return data


def test_profile_is_created_with_normalized_sections(monkeypatch) -> None:
    flow = make_flow(monkeypatch)

    result = asyncio.run(flow.async_step_user(profile_input()))

    assert result["title"] == "Example Supermarket"
    assert result["data"]["sections"] == [
        {"name": "Produce", "hints": "fruit"},
        {"name": "Dairy", "hints": "milk"},
    ]


def test_profile_name_is_trimmed(monkeypatch) -> None:
    flow = make_flow(monkeypatch)

    asyncio.run(flow.async_step_user(profile_input(profile_name="  Shop  ")))

    assert flow.created["title"] == "Shop"
    assert flow.created["data"]["profile_name"] == "Shop"


def test_blank_profile_name_is_rejected(monkeypatch) -> None:
    flow = make_flow(monkeypatch)

    result = asyncio.run(flow.async_step_user(profile_input(profile_name="   ")))

    assert result["type"] == "form"
    assert result["errors"] == {"profile_name": "empty_profile_name"}
    assert flow.created is None


def test_duplicate_profile_name_is_reported_on_the_field(monkeypatch) -> None:
    flow = make_flow(
        monkeypatch,
        current_entries=[other_entry("other", "Example Supermarket")],
    )

    result = asyncio.run(flow.async_step_user(profile_input()))

    assert result["type"] == "form"
    assert result["errors"] == {"profile_name": "duplicate_profile_name"}
    assert flow.created is None


def test_profile_names_are_compared_case_insensitively(monkeypatch) -> None:
    flow = make_flow(
        monkeypatch, current_entries=[other_entry("other", "example SUPERMARKET")]
    )

    result = asyncio.run(flow.async_step_user(profile_input()))

    assert result["errors"] == {"profile_name": "duplicate_profile_name"}


def test_reconfigure_to_a_name_used_by_another_profile_is_rejected(
    monkeypatch,
) -> None:
    entry = other_entry("mine", "My shop")
    flow = make_flow(
        monkeypatch,
        existing_entry=entry,
        current_entries=[entry, other_entry("theirs", "Their shop")],
    )

    result = asyncio.run(
        flow.async_step_reconfigure(profile_input(profile_name="Their shop"))
    )

    assert result["errors"] == {"profile_name": "duplicate_profile_name"}
    assert flow.updated is None


def test_reconfigure_keeping_its_own_name_is_allowed(monkeypatch) -> None:
    """The reconfigured entry must not collide with itself."""

    entry = other_entry("mine", "My shop")
    flow = make_flow(monkeypatch, existing_entry=entry, current_entries=[entry])

    asyncio.run(flow.async_step_reconfigure(profile_input(profile_name="My shop")))

    assert flow.updated["title"] == "My shop"


def test_rename_frees_the_old_name_for_a_new_profile(monkeypatch) -> None:
    """A name-derived unique ID would wrongly keep blocking the old name."""

    renamed = other_entry("mine", "Shop B")
    flow = make_flow(monkeypatch, current_entries=[renamed])

    result = asyncio.run(flow.async_step_user(profile_input(profile_name="Shop A")))

    assert result["title"] == "Shop A"


def test_reconfigure_updates_the_entry_title(monkeypatch) -> None:
    entry = SimpleNamespace(
        entry_id="synthetic-entry",
        title="Old name",
        data={"profile_name": "Old name"},
    )
    flow = make_flow(monkeypatch, existing_entry=entry)

    asyncio.run(flow.async_step_reconfigure(profile_input(profile_name="New name")))

    assert flow.updated["title"] == "New name"
    assert flow.updated["data_updates"]["profile_name"] == "New name"


def test_reconfigure_takes_the_update_path_not_the_create_path(monkeypatch) -> None:
    entry = other_entry("synthetic-entry", "Shop")
    flow = make_flow(monkeypatch, existing_entry=entry, current_entries=[entry])

    asyncio.run(flow.async_step_reconfigure(profile_input(profile_name="Shop")))

    assert flow.created is None
    assert flow.updated["data_updates"]["profile_name"] == "Shop"
    assert flow.updated["title"] == "Shop"


def test_invalid_route_returns_section_error(monkeypatch) -> None:
    flow = make_flow(monkeypatch)

    result = asyncio.run(flow.async_step_user(profile_input(sections="A | x | y")))

    assert result["errors"] == {"sections": "malformed_sections"}


def test_missing_dependency_returns_base_error(monkeypatch) -> None:
    flow = make_flow(monkeypatch)

    result = asyncio.run(
        flow.async_step_user(profile_input(source_entity_id="todo.missing"))
    )

    assert result["errors"] == {"base": "source_not_found"}
