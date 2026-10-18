"""UI-only configuration flow."""

from __future__ import annotations

import re
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
    CONF_PRICE_CHAIN,
    CONF_PRICE_STORE_ID,
    CONF_PROFILE_NAME,
    CONF_SECTIONS,
    CONF_SHOW_COMPLETED,
    CONF_SOURCE_ENTITY_ID,
    CONFIG_ENTRY_VERSION,
    DEFAULT_DISPLAY_SECTION_PREFIX,
    DEFAULT_PRICE_CHAIN,
    DEFAULT_SHOW_COMPLETED,
    DOMAIN,
)
from .models import (
    RouteParseError,
    RouteSection,
    normalize_summary,
    parse_sections,
    serialize_sections,
)
from .prices import (
    CHAIN_PAKNSAVE,
    CHAINS,
    PriceError,
    async_list_paknsave_stores,
)


def _store_key(name: str) -> str:
    """Normalise a store name for comparison.

    The retailer writes "PAK'nSAVE Ormiston" while people (and the profile
    titles they already use) write "PakNSave Ormiston", so apostrophes and
    punctuation must not affect matching.
    """

    return " ".join(re.sub(r"[^\w\s]+", "", normalize_summary(name)).split())


DEFAULT_SECTIONS = """Produce | fruit and vegetables
Bakery | bread and bakery
Pantry | cereal, pasta, rice, canned foods
Frozen | frozen food
Dairy | milk, cheese, yoghurt
Household | cleaning supplies"""


class SmartShoppingRouteConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure a store route profile."""

    VERSION = CONFIG_ENTRY_VERSION

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
            name = str(user_input.get(CONF_PROFILE_NAME, "")).strip()
            try:
                if not name:
                    raise ValueError("empty_profile_name")
                sections = parse_sections(user_input[CONF_SECTIONS])
                self._validate_entities(user_input)
                price_store_id = await self._async_resolve_store(user_input)
            except RouteParseError as err:
                errors[CONF_SECTIONS] = err.code
            except ValueError as err:
                key = str(err)
                if key == "empty_profile_name":
                    field = CONF_PROFILE_NAME
                elif key.startswith("price_store"):
                    field = CONF_PRICE_STORE_ID
                else:
                    field = "base"
                errors[field] = key
            else:
                data = {
                    **user_input,
                    CONF_PROFILE_NAME: name,
                    CONF_PRICE_STORE_ID: price_store_id,
                    CONF_SECTIONS: [
                        {"name": section.name, "hints": section.hints}
                        for section in sections
                    ],
                }
                if entry:
                    if self._name_taken(name, exclude_entry_id=entry.entry_id):
                        errors[CONF_PROFILE_NAME] = "duplicate_profile_name"
                    else:
                        return self.async_update_reload_and_abort(
                            entry, data_updates=data, title=name
                        )
                elif self._name_taken(name):
                    errors[CONF_PROFILE_NAME] = "duplicate_profile_name"
                else:
                    return self.async_create_entry(title=name, data=data)

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
                        RouteSection.from_mapping(raw)
                        for raw in defaults[CONF_SECTIONS]
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
            vol.Optional(
                CONF_PRICE_CHAIN,
                default=defaults.get(CONF_PRICE_CHAIN, DEFAULT_PRICE_CHAIN),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(CHAINS),
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key="price_chain",
                )
            ),
            vol.Optional(
                CONF_PRICE_STORE_ID,
                default=defaults.get(CONF_PRICE_STORE_ID, ""),
            ): selector.TextSelector(),
        })
        return self.async_show_form(
            step_id="reconfigure" if reconfigure else "user",
            data_schema=schema,
            errors=errors,
        )

    def _name_taken(self, name: str, *, exclude_entry_id: str | None = None) -> bool:
        """Names must be unique because they become entity names.

        Uniqueness is checked against current profile names rather than a
        name-derived unique ID: a profile can be renamed, so a unique ID built
        from its original name would block the wrong names afterwards.
        """

        wanted = normalize_summary(name)
        for other in self._async_current_entries():
            if exclude_entry_id is not None and other.entry_id == exclude_entry_id:
                continue
            existing = other.data.get(CONF_PROFILE_NAME) or other.title
            if normalize_summary(str(existing)) == wanted:
                return True
        return False

    async def _async_resolve_store(self, user_input: dict[str, Any]) -> str:
        """Resolve the store selection into a retailer store ID.

        Pak'nSave prices differ per store, so a store is required there and is
        resolved from a name or ID. Woolworths' anonymous search is not
        store-scoped, so no store is kept for it.
        """

        chain = user_input.get(CONF_PRICE_CHAIN, DEFAULT_PRICE_CHAIN)
        store = str(user_input.get(CONF_PRICE_STORE_ID) or "").strip()
        if chain != CHAIN_PAKNSAVE:
            return ""
        if not store:
            raise ValueError("price_store_required")
        try:
            stores = await async_list_paknsave_stores(self.hass)
        except PriceError as err:
            raise ValueError("price_store_unavailable") from err
        if store in stores:
            return store
        wanted = _store_key(store)
        matches = [
            store_id
            for store_id, store_name in stores.items()
            if _store_key(store_name) == wanted
        ]
        if not matches:
            matches = [
                store_id
                for store_id, store_name in stores.items()
                if wanted in _store_key(store_name)
            ]
        if not matches:
            raise ValueError("price_store_not_found")
        if len(matches) > 1:
            raise ValueError("price_store_ambiguous")
        return matches[0]

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
