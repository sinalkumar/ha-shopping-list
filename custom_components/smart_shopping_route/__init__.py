"""Smart Shopping Route integration setup and services."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.target import (
    TargetSelection,
    async_extract_referenced_entity_ids,
)

from .const import (
    ATTR_GROCER_PRODUCT_ID,
    ATTR_INCLUDE_MANUAL,
    ATTR_ITEM_UID,
    ATTR_PRODUCT_ID,
    ATTR_QUERY,
    ATTR_REMEMBER,
    ATTR_SECTION,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    PLATFORMS,
    SERVICE_CLEAR_CACHE,
    SERVICE_FORGET_ITEM_SECTION,
    SERVICE_PIN_ITEM_PRODUCT,
    SERVICE_RECLASSIFY_ALL,
    SERVICE_REFRESH,
    SERVICE_SET_ITEM_SECTION,
    SERVICE_UNPIN_ITEM_PRODUCT,
)
from .coordinator import RouteCoordinator
from .models import section_names
from .storage import PriceStore, async_pop_store


async def async_setup(hass: HomeAssistant, _config: dict[str, Any]) -> bool:
    """Expose the optional card and permission-checked route read endpoint."""
    from .ui import async_register_ui

    await async_register_ui(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one route profile."""
    hass.data.setdefault(DOMAIN, {})
    coordinator = RouteCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except HomeAssistantError as err:
        raise ConfigEntryNotReady("Source to-do entity is not ready") from err
    hass.data[DOMAIN][entry.entry_id] = coordinator
    try:
        _async_register_services(hass)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # Never leave a live source subscription and background tasks behind.
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await coordinator.async_shutdown()
        raise
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
            SERVICE_PIN_ITEM_PRODUCT,
            SERVICE_UNPIN_ITEM_PRODUCT,
        ):
            hass.services.async_remove(DOMAIN, service)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the stored classification cache of a removed profile."""
    # A failed unload leaves the coordinator and its pending delayed save alive;
    # tear it down first so nothing can recreate the cache file afterwards.
    coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if coordinator is not None:
        await coordinator.async_shutdown()
    # Remove through the profile's own store instance: Store cancels its
    # final-write listener per instance, so a fresh Store could not prevent a
    # pending write from recreating the file during shutdown.
    await async_pop_store(hass, entry.entry_id).async_remove()
    # Pins and price history are user data too, and must not outlive the profile.
    await PriceStore(hass, entry.entry_id).async_remove()


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry data written by an older version."""
    if entry.version > CONFIG_ENTRY_VERSION:
        # Downgrade: refuse rather than run on data we cannot interpret.
        return False
    return True


def _route_entity_ids(hass: HomeAssistant, call: ServiceCall) -> set[str]:
    """Resolve every target form: entity, device, area, floor, or label."""

    selected = async_extract_referenced_entity_ids(hass, TargetSelection(call.data))
    registry = er.async_get(hass)
    entity_ids: set[str] = set()
    for entity_id in selected.referenced | selected.indirectly_referenced:
        entry = registry.async_get(entity_id)
        if entry is not None and entry.domain == "todo" and entry.platform == DOMAIN:
            entity_ids.add(entity_id)
    return entity_ids


def _coordinator_for_call(hass: HomeAssistant, call: ServiceCall) -> RouteCoordinator:
    entity_ids = _route_entity_ids(hass, call)
    if len(entity_ids) != 1:
        raise ServiceValidationError("Target exactly one Smart Shopping Route entity")
    entity_id = entity_ids.pop()
    registry_entry = er.async_get(hass).async_get(entity_id)
    entry_id = registry_entry.config_entry_id if registry_entry else None
    coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
    if coordinator is None:
        raise ServiceValidationError("Route entity was not found")
    return coordinator


def _item_uid(call: ServiceCall) -> str:
    uid = call.data.get(ATTR_ITEM_UID)
    if not uid:
        raise ServiceValidationError("An item UID is required")
    return uid


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        return

    async def authorized(call: ServiceCall) -> RouteCoordinator:
        coordinator = _coordinator_for_call(hass, call)
        user_id = getattr(getattr(call, "context", None), "user_id", None)
        if user_id:
            # Permission checks must cover the resolved route even when the
            # caller targets its device/area/label rather than an entity_id.
            targets = {coordinator.source.entity_id, *_route_entity_ids(hass, call)}
            user = await hass.auth.async_get_user(user_id)
            if user is None or any(
                not user.permissions.check_entity(target, POLICY_CONTROL)
                for target in targets
                if target
            ):
                raise ServiceValidationError(
                    "Not authorized to control this route and source"
                )
        return coordinator

    async def refresh(call: ServiceCall) -> None:
        await (await authorized(call)).async_refresh()

    async def set_section(call: ServiceCall) -> None:
        coordinator = await authorized(call)
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
            await (await authorized(call)).async_forget(_item_uid(call))
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def reclassify(call: ServiceCall) -> None:
        await (await authorized(call)).async_reclassify_all()

    async def pin_product(call: ServiceCall) -> None:
        coordinator = await authorized(call)
        if not coordinator.prices.enabled:
            raise ServiceValidationError("Pricing is not enabled for this route")
        try:
            item = coordinator.item_by_uid(_item_uid(call))
            await coordinator.prices.async_pin(
                item.summary or "",
                query=call.data.get(ATTR_QUERY, ""),
                product_id=call.data.get(ATTR_PRODUCT_ID),
                grocer_product_id=call.data.get(ATTR_GROCER_PRODUCT_ID),
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def unpin_product(call: ServiceCall) -> None:
        coordinator = await authorized(call)
        try:
            item = coordinator.item_by_uid(_item_uid(call))
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        await coordinator.prices.async_unpin(item.summary or "")

    async def clear(call: ServiceCall) -> None:
        await (await authorized(call)).async_clear_cache(call.data[ATTR_INCLUDE_MANUAL])

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH,
        refresh,
        schema=cv.make_entity_service_schema({}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ITEM_SECTION,
        set_section,
        schema=cv.make_entity_service_schema({
            vol.Required(ATTR_ITEM_UID): cv.string,
            vol.Required(ATTR_SECTION): cv.string,
            vol.Optional(ATTR_REMEMBER, default=True): cv.boolean,
        }),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORGET_ITEM_SECTION,
        forget,
        schema=cv.make_entity_service_schema({
            vol.Required(ATTR_ITEM_UID): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECLASSIFY_ALL,
        reclassify,
        schema=cv.make_entity_service_schema({}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PIN_ITEM_PRODUCT,
        pin_product,
        schema=cv.make_entity_service_schema({
            vol.Required(ATTR_ITEM_UID): cv.string,
            vol.Optional(ATTR_QUERY, default=""): cv.string,
            vol.Optional(ATTR_PRODUCT_ID): cv.string,
            vol.Optional(ATTR_GROCER_PRODUCT_ID): vol.Coerce(int),
        }),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UNPIN_ITEM_PRODUCT,
        unpin_product,
        schema=cv.make_entity_service_schema({
            vol.Required(ATTR_ITEM_UID): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_CACHE,
        clear,
        schema=cv.make_entity_service_schema({
            vol.Optional(ATTR_INCLUDE_MANUAL, default=False): cv.boolean,
        }),
    )
