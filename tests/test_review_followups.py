"""Independent-review follow-ups and real-schema mirror regression coverage."""

import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.todo import TodoItem
from homeassistant.components.todo.const import TodoListEntityFeature
from homeassistant.exceptions import ServiceValidationError

from smart_shopping_route.grocer import (
    GrocerData,
    GrocerMatch,
    GrocerPrice,
    GrocerProduct,
)
from smart_shopping_route.models import RouteSection, strip_display_prefix
from smart_shopping_route.prices import PriceQuote, ProductPin, WoolworthsPriceClient
from test_grocer import FakeResponse, FakeSession
from test_prices import FakeResponse as PriceResponse
from test_prices import FakeSession as PriceSession
from test_prices import make_price_coordinator, woolworths_body
from test_todo_proxy import FakePrices, make_entity


def mirror(tmp_path):
    class Hass:
        config = SimpleNamespace(path=lambda _: str(tmp_path))

        async def async_add_executor_job(self, fn, *args):
            return fn(*args)

    return GrocerData(Hass())


def write_history(path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    pq.write_table(
        pa.table({
            "updated_at": pa.array(
                [1700000000000, 1700000060000], type=pa.timestamp("ms", tz="UTC")
            ),
            "store_id": [2, 2],
            "price_cent": [400, 500],
        }),
        path,
        compression="gzip",
        row_group_size=1,
    )


def test_history_validation_and_read_use_the_real_public_schema(tmp_path):
    data = mirror(tmp_path)
    target = data._dir / "history_1.parquet"
    write_history(target)
    data._validate_mirror(target, target.name)
    assert data.history(1, 2) == [(1700000000.0, 4.0), (1700000060.0, 5.0)]


def test_invalid_history_download_retains_last_valid_asset(tmp_path):
    data = mirror(tmp_path)
    target = data._dir / "history_1.parquet"
    write_history(target)
    before = target.read_bytes()
    os.utime(target, (0, 0))
    data._session = FakeSession([FakeResponse(200, b"not parquet")])
    assert asyncio.run(data.async_sync_history(1))
    assert target.read_bytes() == before
    assert len(data.history(1, 2)) == 2
    assert not list(data._dir.glob("*.part"))


def test_woolworths_store_history_never_becomes_online_discount(monkeypatch):
    quote = PriceQuote("woolworths", "online-1", "Milk 2L", 4)
    coordinator, _ = make_price_coordinator(monkeypatch, ["Milk"], {"Milk": quote})
    asyncio.run(coordinator._async_update_data())
    match = GrocerMatch(
        GrocerProduct(1, "Milk 2L"), GrocerPrice(1, 8, None), [(time.time() - 3600, 8)]
    )
    coordinator._apply_history("milk", match, quote)
    assert [p.price for p in coordinator.snapshot.history["milk"]] == [4]
    assert [p.price for p in coordinator.snapshot.store_history["milk"]] == [8]
    assert coordinator.snapshot.reference_price("milk") is None
    assert coordinator.snapshot.drop_for("milk") is None


def test_removed_items_stop_exempting_mirror_files_from_pruning(monkeypatch):
    coordinator, _ = make_price_coordinator(monkeypatch, ["Milk"], {})
    coordinator._matched_products = {"milk": 1, "old item": 2}
    assert coordinator._retained_products() == {1}
    coordinator._summaries = lambda: []
    assert coordinator._retained_products() == set()
    coordinator._pins["old item"] = ProductPin("paknsave", grocer_product_id=2)
    assert coordinator._retained_products() == {2}


def test_verified_history_product_survives_a_retailer_switch(monkeypatch):
    coordinator, _ = make_price_coordinator(monkeypatch, ["Milk"], {})
    coordinator._bind_history("milk", PriceQuote("paknsave", "old", "Old Milk", 10))
    coordinator._bind_history(
        "milk", PriceQuote("paknsave", "new", "New Milk", 4), verified_history_product=7
    )
    assert coordinator._history_products["milk"] == 7
    assert coordinator._identities["milk"].endswith(',"new",7]')


@pytest.mark.parametrize("note", ["Keep chilled\n\n", "Keep chilled  ", "", "\n\n"])
def test_native_price_roundtrip_preserves_note_whitespace(note):
    entity, coordinator = make_entity()
    coordinator.source.supported_features |= int(
        TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )
    coordinator.items[0].description = note
    coordinator.prices = FakePrices({"Milk": PriceQuote("paknsave", "1", "Milk", 4)})
    displayed = next(item for item in entity.todo_items if item.uid == "uid-1")
    asyncio.run(entity.async_update_todo_item(displayed))
    assert coordinator.source.updated[0].description == note


def test_native_stale_price_does_not_claim_a_current_discount(monkeypatch):
    entity, coordinator = make_entity()
    coordinator.prices = FakePrices({
        "Milk": PriceQuote("woolworths", "1", "Milk", 4, was_price=6)
    })
    monkeypatch.setattr(
        FakePrices,
        "snapshot",
        property(lambda _self: SimpleNamespace(stale_for=lambda _key: True)),
    )
    description = entity._price_description(coordinator.items[0])
    assert "STALE" in description
    assert "was $" not in description
    assert "previously seen" not in description


def test_generated_price_edits_are_rejected_not_ignored():
    entity, coordinator = make_entity()
    coordinator.prices = FakePrices({"Milk": PriceQuote("paknsave", "1", "Milk", 4)})
    displayed = next(item for item in entity.todo_items if item.uid == "uid-1")
    displayed.description = displayed.description.replace("$4.00", "$1.00")
    with pytest.raises(ServiceValidationError, match="read-only"):
        asyncio.run(entity.async_update_todo_item(displayed))
    assert coordinator.source.updated == []


def test_overlapping_prefixes_strip_the_actual_section_only():
    assert (
        strip_display_prefix("Dairy · Frozen · Milk", ["Dairy", "Dairy · Frozen"])
        == "Milk"
    )
    entity, coordinator = make_entity(prefix=True)
    coordinator.sections = (*coordinator.sections, RouteSection("Dairy · Frozen"))
    coordinator.section_for = lambda _item: "Dairy · Frozen"
    displayed = next(item for item in entity.todo_items if item.uid == "uid-1")
    asyncio.run(entity.async_update_todo_item(displayed))
    assert coordinator.source.updated[0].summary == "Milk"
    # A longer configured section must not swallow literal source name text.
    coordinator.section_for = lambda _item: "Dairy"
    coordinator.items[0].summary = "Frozen · Milk"
    displayed = next(item for item in entity.todo_items if item.uid == "uid-1")
    asyncio.run(entity.async_update_todo_item(displayed))
    assert coordinator.source.updated[-1].summary == "Frozen · Milk"


def test_native_mutation_requires_source_control():
    entity, coordinator = make_entity()
    entity._context = SimpleNamespace(user_id="reader")
    entity.hass = SimpleNamespace(
        auth=SimpleNamespace(
            async_get_user=AsyncMock(
                return_value=SimpleNamespace(
                    permissions=SimpleNamespace(check_entity=lambda *_args: False)
                )
            )
        )
    )
    for method, value in [
        (entity.async_create_todo_item, TodoItem(summary="Milk")),
        (entity.async_update_todo_item, TodoItem(uid="uid-1", summary="Milk")),
        (entity.async_delete_todo_items, ["uid-1"]),
    ]:
        with pytest.raises(ServiceValidationError, match="Not authorized"):
            asyncio.run(method(value))
    assert (
        not coordinator.source.updated
        and not coordinator.source.created
        and not coordinator.source.deleted
    )


def test_unlabelled_multiple_woolworths_variants_are_not_guessed():
    client = WoolworthsPriceClient(SimpleNamespace())
    client._session = PriceSession([
        PriceResponse(
            200,
            woolworths_body([
                {
                    "sku": "1",
                    "productName": "Example Juice 1L",
                    "variants": [
                        {"variantPrice": {"sellingPrice": 4}},
                        {"variantPrice": {"sellingPrice": 8}},
                    ],
                }
            ]),
        )
    ])
    assert asyncio.run(client.async_lookup("juice")) is None
