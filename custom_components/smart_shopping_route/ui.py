"""Permission-checked, non-recorder route data for the optional shopping card."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.components.todo import TodoItemStatus
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_DISPLAY_SECTION_PREFIX,
    CONF_SHOW_COMPLETED,
    DEFAULT_DISPLAY_SECTION_PREFIX,
    DEFAULT_SHOW_COMPLETED,
    DOMAIN,
)
from .models import normalize_summary, sort_items
from .stores import HISTORY_STORES

CARD_URL = "/smart_shopping_route/route-card.js"


def route_payload(coordinator: Any, *, can_control: bool) -> dict[str, Any]:
    """Names/notes live only in an authorized response, not entity attributes."""
    prices = coordinator.prices
    snapshot = prices.snapshot
    rows = []
    for item in sort_items(
        coordinator.items,
        coordinator.sections,
        {
            f"uid:{item.uid}": coordinator.section_for(item)
            for item in coordinator.items
        },
        show_completed=coordinator.entry.data.get(
            CONF_SHOW_COMPLETED, DEFAULT_SHOW_COMPLETED
        ),
        completed_status=TodoItemStatus.COMPLETED,
    ):
        key = normalize_summary(item.summary or "")
        completed = item.status == TodoItemStatus.COMPLETED
        quote = prices.quote_for(item.summary or "") if not completed else None
        previous = snapshot.reference_point(key) if quote else None
        pin = prices.pin_for(item.summary or "")
        rows.append({
            "uid": item.uid,
            "summary": item.summary or "",
            "display_prefix": f"{coordinator.section_for(item)} · "
            if coordinator.entry.data.get(
                CONF_DISPLAY_SECTION_PREFIX, DEFAULT_DISPLAY_SECTION_PREFIX
            )
            else "",
            "notes": item.description or "",
            "completed": completed,
            "section": coordinator.section_for(item),
            "price": quote.as_dict() if quote else None,
            "stale": snapshot.stale_for(key) if quote else False,
            "observed_at": snapshot.observed_at.get(key),
            "previous": {"price": previous.price, "ts": previous.ts}
            if previous
            else None,
            "history": [
                {"ts": point.ts, "price": point.price}
                for point in snapshot.points_for(key)
            ],
            "store_history": [
                {"ts": point.ts, "price": point.price}
                for point in snapshot.store_history.get(key, [])
            ],
            "pin": {
                "query": pin.query,
                "product_id": pin.product_id,
                "grocer_product_id": pin.grocer_product_id,
            }
            if pin
            else None,
        })
    return {
        "name": coordinator.entry.title,
        "available": coordinator.available,
        "can_control": can_control and coordinator.available,
        "source_features": int(coordinator.source.supported_features),
        "sections": [section.name for section in coordinator.sections],
        "prices_enabled": prices.enabled,
        "price_store": coordinator.entry.data.get("price_store_name")
        or (
            "Woolworths online (not store-specific)"
            if prices.chain == "woolworths"
            else "Current-price store"
        ),
        "history_store": HISTORY_STORES.get(prices.grocer_store_id, ("", ""))[1],
        "quality": prices.status_attributes(),
        "total": snapshot.total,
        "legacy_history_preserved": any(
            identity.startswith("legacy:") for identity in prices._history_archive
        ),
        "items": rows,
    }


async def websocket_get_route(hass: Any, connection: Any, msg: dict[str, Any]) -> None:
    entity_id = msg["entity_id"]
    registry_entry = er.async_get(hass).async_get(entity_id)
    coordinator = None
    if (
        registry_entry
        and registry_entry.platform == DOMAIN
        and entity_id.startswith("todo.")
    ):
        coordinator = hass.data.get(DOMAIN, {}).get(registry_entry.config_entry_id)
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "Shopping route is not loaded")
        return
    permissions = connection.user.permissions
    source_id = coordinator.source.entity_id
    if not all(
        permissions.check_entity(target, POLICY_READ)
        for target in (entity_id, source_id)
    ):
        connection.send_error(
            msg["id"], "unauthorized", "Not authorized to read this route and source"
        )
        return
    can_control = all(
        permissions.check_entity(target, POLICY_CONTROL)
        for target in (entity_id, source_id)
    )
    connection.send_result(
        msg["id"], route_payload(coordinator, can_control=can_control)
    )


async def async_register_ui(hass: Any) -> None:
    from homeassistant.components import websocket_api
    from homeassistant.components.http import StaticPathConfig

    command = websocket_api.websocket_command({
        vol.Required("type"): f"{DOMAIN}/get_route",
        vol.Required("entity_id"): str,
    })(websocket_api.async_response(websocket_get_route))
    websocket_api.async_register_command(hass, command)
    await hass.http.async_register_static_paths([
        StaticPathConfig(
            CARD_URL, str(Path(__file__).parent / "frontend" / "route-card.js"), False
        )
    ])
