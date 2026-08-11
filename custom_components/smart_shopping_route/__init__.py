"""Smart Shopping Route integration setup and services."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import (
    ATTR_INCLUDE_MANUAL,
    ATTR_ITEM,
    ATTR_ITEM_UID,
    ATTR_REMEMBER,
    ATTR_ROUTE_ENTITY_ID,
    ATTR_SECTION,
    DOMAIN,
    PLATFORMS,
    SERVICE_CLEAR_CACHE,
    SERVICE_FORGET_ITEM_SECTION,
    SERVICE_RECLASSIFY_ALL,
    SERVICE_REFRESH,
    SERVICE_SET_ITEM_SECTION,
)
from .coordinator import RouteCoordinator
from .models import section_names


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one route profile."""
    hass.data.setdefault(DOMAIN, {})
    coordinator = RouteCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except Exception as err:
        raise ConfigEntryNotReady("Source to-do entity is not ready") from err
    hass.data[DOMAIN][entry.entry_id] = coordinator
    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        _async_register_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a profile and cancel all integration-owned tasks."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    coordinator: RouteCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.async_shutdown()
    if not hass.data[DOMAIN]:
        for service in (
            SERVICE_REFRESH,
            SERVICE_SET_ITEM_SECTION,
            SERVICE_FORGET_ITEM_SECTION,
            SERVICE_RECLASSIFY_ALL,
            SERVICE_CLEAR_CACHE,
        ):
            hass.services.async_remove(DOMAIN, service)
    return True


def _coordinator_for_call(hass: HomeAssistant, call: ServiceCall) -> RouteCoordinator:
    registry_entry = er.async_get(hass).async_get(call.data[ATTR_ROUTE_ENTITY_ID])
    entry_id = registry_entry.config_entry_id if registry_entry else None
    coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
    if coordinator is None:
        raise ServiceValidationError("Route entity was not found")
    return coordinator


def _item_uid(call: ServiceCall) -> str:
    uid = call.data.get(ATTR_ITEM_UID) or call.data.get(ATTR_ITEM)
    if not uid:
        raise ServiceValidationError("An item UID is required")
    return uid


def _async_register_services(hass: HomeAssistant) -> None:
    base = {vol.Required(ATTR_ROUTE_ENTITY_ID): cv.entity_id}

    async def refresh(call: ServiceCall) -> None:
        await _coordinator_for_call(hass, call).async_refresh()

    async def set_section(call: ServiceCall) -> None:
        coordinator = _coordinator_for_call(hass, call)
        section = call.data[ATTR_SECTION]
        if section not in section_names(coordinator.sections):
            raise ServiceValidationError("Section is not configured for this route")
        try:
            await coordinator.async_set_section(
                _item_uid(call), section, call.data[ATTR_REMEMBER]
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def forget(call: ServiceCall) -> None:
        try:
            await _coordinator_for_call(hass, call).async_forget(_item_uid(call))
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def reclassify(call: ServiceCall) -> None:
        await _coordinator_for_call(hass, call).async_reclassify_all()

    async def clear(call: ServiceCall) -> None:
        await _coordinator_for_call(hass, call).async_clear_cache(
            call.data[ATTR_INCLUDE_MANUAL]
        )

    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, refresh, schema=vol.Schema(base)
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ITEM_SECTION,
        set_section,
        schema=vol.Schema({
            **base,
            vol.Optional(ATTR_ITEM_UID): cv.string,
            vol.Optional(ATTR_ITEM): cv.string,
            vol.Required(ATTR_SECTION): cv.string,
            vol.Optional(ATTR_REMEMBER, default=True): cv.boolean,
        }),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORGET_ITEM_SECTION,
        forget,
        schema=vol.Schema({
            **base,
            vol.Optional(ATTR_ITEM_UID): cv.string,
            vol.Optional(ATTR_ITEM): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RECLASSIFY_ALL, reclassify, schema=vol.Schema(base)
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_CACHE,
        clear,
        schema=vol.Schema({
            **base,
            vol.Optional(ATTR_INCLUDE_MANUAL, default=False): cv.boolean,
        }),
    )
