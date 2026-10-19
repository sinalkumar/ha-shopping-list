"""The card reads private data only after route AND source authorization."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.todo import TodoItem
from homeassistant.components.todo.const import TodoItemStatus

from smart_shopping_route import ui
from smart_shopping_route.models import RouteSection
from smart_shopping_route.price_coordinator import PricePoint
from smart_shopping_route.prices import PriceQuote
from test_prices import make_price_coordinator


def test_real_websocket_schema_and_static_resource_registration():
    hass = SimpleNamespace(
        data={}, http=SimpleNamespace(async_register_static_paths=AsyncMock())
    )
    asyncio.run(ui.async_register_ui(hass))
    _handler, schema = hass.data["websocket_api"]["smart_shopping_route/get_route"]
    message = {
        "id": 1,
        "type": "smart_shopping_route/get_route",
        "entity_id": "todo.example",
    }
    assert schema(message) == message
    resource = hass.http.async_register_static_paths.call_args.args[0][0]
    assert resource.url_path == ui.CARD_URL
    assert resource.path.endswith("frontend/route-card.js")
    assert not resource.cache_headers


def route(monkeypatch):
    prices, _ = make_price_coordinator(
        monkeypatch,
        ["Milk"],
        {
            "Milk": PriceQuote(
                "paknsave", "1", "Example Milk 2L", 4, size="2L", unit_price="$2.00/L"
            )
        },
    )
    asyncio.run(prices._async_update_data())
    return SimpleNamespace(
        entry=SimpleNamespace(
            title="Example store",
            data={"price_store_name": "Example current-price store"},
        ),
        prices=prices,
        items=[
            TodoItem(
                uid="1",
                summary="Milk",
                description="Keep chilled",
                status=TodoItemStatus.NEEDS_ACTION,
            )
        ],
        sections=(RouteSection("Dairy"),),
        section_for=lambda _item: "Dairy",
        available=True,
        source=SimpleNamespace(entity_id="todo.source", supported_features=71),
    )


@pytest.mark.parametrize(
    "configured, expected", [(None, False), (True, True), (False, False)]
)
def test_card_completed_visibility_matches_entity_default(
    monkeypatch, configured, expected
):
    coordinator = route(monkeypatch)
    coordinator.items[0].status = TodoItemStatus.COMPLETED
    if configured is not None:
        coordinator.entry.data[ui.CONF_SHOW_COMPLETED] = configured
    assert bool(ui.route_payload(coordinator, can_control=True)["items"]) is expected


def test_route_data_separates_notes_from_read_only_prices(monkeypatch):
    coordinator = route(monkeypatch)
    payload = ui.route_payload(coordinator, can_control=True)
    assert payload["items"][0]["notes"] == "Keep chilled"
    assert payload["items"][0]["price"]["unit_price"] == "$2.00/L"
    assert payload["quality"]["price_status"] == "current"
    assert payload["can_control"]
    assert not coordinator.entry.data.get("items")


@pytest.mark.parametrize(
    ("deny_read", "deny_control", "error", "control"),
    [
        ("todo.route", None, "unauthorized", None),
        ("todo.source", None, "unauthorized", None),
        (None, "todo.route", None, False),
        (None, "todo.source", None, False),
        (None, None, None, True),
    ],
)
def test_websocket_checks_both_entity_permissions(
    monkeypatch, deny_read, deny_control, error, control
):
    coordinator = route(monkeypatch)
    registry = SimpleNamespace(
        async_get=lambda _eid: SimpleNamespace(
            platform="smart_shopping_route", config_entry_id="e1"
        )
    )
    monkeypatch.setattr(ui.er, "async_get", lambda _hass: registry)
    connection = SimpleNamespace(
        user=SimpleNamespace(
            permissions=SimpleNamespace(
                check_entity=lambda entity, policy: not (
                    (policy == "read" and entity == deny_read)
                    or (policy == "control" and entity == deny_control)
                )
            )
        ),
        send_error=Mock(),
        send_result=Mock(),
    )
    hass = SimpleNamespace(data={"smart_shopping_route": {"e1": coordinator}})
    asyncio.run(
        ui.websocket_get_route(hass, connection, {"id": 1, "entity_id": "todo.route"})
    )
    if error:
        assert connection.send_error.call_args.args[1] == error
        connection.send_result.assert_not_called()
    else:
        assert connection.send_result.call_args.args[1]["can_control"] is control


def test_removed_or_foreign_routes_return_no_private_data(monkeypatch):
    registry = SimpleNamespace(async_get=lambda _eid: None)
    monkeypatch.setattr(ui.er, "async_get", lambda _hass: registry)
    connection = SimpleNamespace(send_error=Mock(), send_result=Mock())
    asyncio.run(
        ui.websocket_get_route(
            SimpleNamespace(data={}), connection, {"id": 1, "entity_id": "todo.missing"}
        )
    )
    assert connection.send_error.call_args.args[1] == "not_found"
    connection.send_result.assert_not_called()


def test_historical_reference_is_dated_recent_and_not_an_ancient_high(monkeypatch):
    coordinator = route(monkeypatch)
    now = time.time()
    coordinator.prices.snapshot.history["milk"] = [
        PricePoint(now - 40 * 86400, 20),
        PricePoint(now - 3600, 3),
    ]
    payload = ui.route_payload(coordinator, can_control=False)
    assert payload["items"][0]["previous"]["price"] == 3
    assert coordinator.prices.snapshot.reference_price("milk") is None
    coordinator.prices.snapshot.history["milk"] = [PricePoint(now - 40 * 86400, 20)]
    assert (
        ui.route_payload(coordinator, can_control=False)["items"][0]["previous"] is None
    )


def test_stale_prices_do_not_claim_current_savings(monkeypatch):
    coordinator = route(monkeypatch)
    coordinator.prices.snapshot.observed_at["milk"] = time.time() - 24 * 3600
    coordinator.prices.snapshot.quotes["milk"] = PriceQuote(
        "paknsave", "1", "Milk", 4, was_price=6
    )
    payload = ui.route_payload(coordinator, can_control=True)
    assert payload["quality"]["price_status"] == "stale"
    assert not payload["quality"]["complete"]
    assert payload["items"][0]["stale"]
    assert coordinator.prices.snapshot.savings == 0
    assert coordinator.prices.snapshot.specials == []


def test_a_profile_without_a_stored_name_still_names_its_store(monkeypatch) -> None:
    """Entries written before 0.8.8 stored only the ids, not the labels."""

    coordinator = route(monkeypatch)
    coordinator.entry.data.pop("price_store_name")
    coordinator.prices.grocer_store_id = 228

    payload = ui.route_payload(coordinator, can_control=True)

    # Both ids name the same physical store, so the history name is faithful.
    assert payload["price_store"] == payload["history_store"]
    assert payload["price_store"] == "PAK'nSAVE Ormiston"
    assert "Current-price store" not in payload["price_store"]


def test_woolworths_without_a_stored_name_uses_the_online_label(monkeypatch) -> None:
    coordinator = route(monkeypatch)
    coordinator.entry.data.pop("price_store_name")
    coordinator.prices.chain = "woolworths"

    payload = ui.route_payload(coordinator, can_control=True)

    assert payload["price_store"] == "Woolworths online (not store-specific)"


def test_an_unnamed_store_falls_back_to_a_neutral_label(monkeypatch) -> None:
    coordinator = route(monkeypatch)
    coordinator.entry.data.pop("price_store_name")
    coordinator.prices.grocer_store_id = None

    payload = ui.route_payload(coordinator, can_control=True)

    assert payload["price_store"] == "Current-price store"
