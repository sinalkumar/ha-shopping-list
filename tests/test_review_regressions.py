"""Product invariants found by the holistic review; synthetic data only."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from smart_shopping_route.price_coordinator import PriceCoordinator, PricePoint
from smart_shopping_route.prices import PriceQuote
from test_prices import fake_entry, make_price_coordinator


def test_product_switch_never_creates_a_discount(monkeypatch):
    prices = {"Milk": PriceQuote("paknsave", "A", "Brand A Milk 2L", 10)}
    coordinator, _ = make_price_coordinator(monkeypatch, ["Milk"], prices)
    asyncio.run(coordinator._async_update_data())
    prices["Milk"] = PriceQuote("paknsave", "B", "Brand B Milk 1L", 3)
    coordinator._force_full = True
    asyncio.run(coordinator._async_update_data())
    assert coordinator.snapshot.drop_for("milk") is None
    assert [p.price for p in coordinator.snapshot.history["milk"]] == [3]
    assert coordinator._history_archive


def test_scope_change_quarantines_but_preserves_history(monkeypatch):
    coordinator, _ = make_price_coordinator(
        monkeypatch, ["Milk"], {"Milk": PriceQuote("paknsave", "A", "Milk", 10)}
    )
    asyncio.run(coordinator._async_update_data())
    asyncio.run(coordinator._async_persist())
    saved = coordinator._price_store.saved[-1]
    entry = fake_entry()
    entry.data = {"price_chain": "paknsave", "price_store_id": "different-store"}
    replacement = PriceCoordinator(SimpleNamespace(), entry, summaries=lambda: ["Milk"])
    replacement._price_store.initial = saved
    asyncio.run(replacement.async_load())
    assert replacement.snapshot.history == {}
    assert replacement._history_archive
    assert replacement._last_store_sync == 0


def test_repin_invalidates_history_and_backfill(monkeypatch):
    coordinator, _ = make_price_coordinator(monkeypatch, ["Milk"], {})
    coordinator._backfilled.add("milk")
    coordinator.snapshot.history["milk"] = [PricePoint(100, 10)]
    coordinator.async_refresh = AsyncMock()
    asyncio.run(
        coordinator.async_pin(
            "Milk", query="Different milk", product_id="B", grocer_product_id=99
        )
    )
    assert "milk" not in coordinator._backfilled
    assert not coordinator.snapshot.history.get("milk")


def test_parquet_preserves_all_row_groups(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    from smart_shopping_route.parquet import read_columns

    path = tmp_path / "multi.parquet"
    pq.write_table(
        pa.table({"id": [1, 2, 3, 4, 5, 6], "price": [100, 200, 300, 400, 500, 600]}),
        path,
        row_group_size=2,
        compression="gzip",
    )
    assert pq.ParquetFile(path).metadata.num_row_groups == 3
    assert read_columns(str(path), ["id", "price"]) == {
        "id": [1, 2, 3, 4, 5, 6],
        "price": [100, 200, 300, 400, 500, 600],
    }
