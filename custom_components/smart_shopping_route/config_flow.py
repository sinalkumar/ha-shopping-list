"""UI-only configuration flow."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.ai_task import AITaskEntityFeature
from homeassistant.components.todo.const import TodoListEntityFeature
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    CONF_AI_TASK_ENTITY_ID,
    CONF_DISPLAY_SECTION_PREFIX,
    CONF_PROFILE_NAME,
    CONF_SECTIONS,
    CONF_SHOW_COMPLETED,
    CONF_SOURCE_ENTITY_ID,
    DEFAULT_DISPLAY_SECTION_PREFIX,
    DEFAULT_SHOW_COMPLETED,
    DOMAIN,
)
from .models import RouteParseError, RouteSection, parse_sections, serialize_sections

DEFAULT_SECTIONS = """Produce | fruit and vegetables
Bakery | bread and bakery
Pantry | cereal, pasta, rice, canned foods
Frozen | frozen food
Dairy | milk, cheese, yoghurt
Household | cleaning supplies"""


class SmartShoppingRouteConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure a store route profile."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self._async_step_profile(user_input, reconfigure=False)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self._async_step_profile(user_input, reconfigure=True)

    async def _async_step_profile(
        self, user_input: dict[str, Any] | None, *, reconfigure: bool
    ) -> FlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry() if reconfigure else None
        defaults = dict(entry.data) if entry else {}
        if user_input is not None:
            try:
                sections = parse_sections(user_input[CONF_SECTIONS])
                self._validate_entities(user_input)
            except RouteParseError as err:
                errors[CONF_SECTIONS] = err.code
            except ValueError as err:
                errors["base"] = str(err)
            else:
                data = {
                    **user_input,
                    CONF_SECTIONS: [
                        {"name": section.name, "hints": section.hints}
                        for section in sections
                    ],
                }
                if entry:
                    return self.async_update_reload_and_abort(entry, data_updates=data)
                return self.async_create_entry(
                    title=user_input[CONF_PROFILE_NAME], data=data
                )

        schema = vol.Schema({
            vol.Required(
                CONF_PROFILE_NAME,
                default=defaults.get(CONF_PROFILE_NAME, "Example Supermarket"),
            ): selector.TextSelector(),
            vol.Required(
                CONF_SOURCE_ENTITY_ID,
                default=defaults.get(CONF_SOURCE_ENTITY_ID),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="todo")),
            vol.Required(
                CONF_AI_TASK_ENTITY_ID,
                default=defaults.get(CONF_AI_TASK_ENTITY_ID),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="ai_task")),
            vol.Required(
                CONF_SECTIONS,
                default=(
                    serialize_sections(
                        RouteSection(**raw) for raw in defaults[CONF_SECTIONS]
                    )
                    if defaults.get(CONF_SECTIONS)
                    else DEFAULT_SECTIONS
                ),
            ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
            vol.Optional(
                CONF_SHOW_COMPLETED,
                default=defaults.get(CONF_SHOW_COMPLETED, DEFAULT_SHOW_COMPLETED),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_DISPLAY_SECTION_PREFIX,
                default=defaults.get(
                    CONF_DISPLAY_SECTION_PREFIX, DEFAULT_DISPLAY_SECTION_PREFIX
                ),
            ): selector.BooleanSelector(),
        })
        return self.async_show_form(
            step_id="reconfigure" if reconfigure else "user",
            data_schema=schema,
            errors=errors,
        )

    def _validate_entities(self, user_input: dict[str, Any]) -> None:
        source_id = user_input[CONF_SOURCE_ENTITY_ID]
        ai_id = user_input[CONF_AI_TASK_ENTITY_ID]
        source = self.hass.states.get(source_id)
        ai_task = self.hass.states.get(ai_id)
        if source is None:
            raise ValueError("source_not_found")
        registry_entry = er.async_get(self.hass).async_get(source_id)
        if registry_entry and registry_entry.platform == DOMAIN:
            raise ValueError("source_is_route")
        source_features = int(source.attributes.get("supported_features", 0))
        if not source_features & TodoListEntityFeature.UPDATE_TODO_ITEM:
            raise ValueError("source_no_update")
        if ai_task is None:
            raise ValueError("ai_task_not_found")
        ai_features = int(ai_task.attributes.get("supported_features", 0))
        if not ai_features & AITaskEntityFeature.GENERATE_DATA:
            raise ValueError("ai_task_no_generate_data")
