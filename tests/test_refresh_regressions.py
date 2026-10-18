"""End-to-end coordinator lifecycle regressions, without production mutations."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from smart_shopping_route.grocer import GrocerData, GrocerError
from smart_shopping_route.price_coordinator import PriceCoordinator
from smart_shopping_route.prices import PriceQuote
from test_coordinator import item, make_coordinator
from test_prices import fake_entry, make_price_coordinator


def test_interval_uses_sync_persistence_and_resets_force():
    async def run():
        coordinator = PriceCoordinator(
            SimpleNamespace(loop=asyncio.get_running_loop(), is_stopping=False),
            fake_entry(),
            summaries=lambda: ["Milk"],
        )
        coordinator._client = SimpleNamespace(
            async_lookup=AsyncMock(
                return_value=PriceQuote("paknsave", "1", "Milk 2L", 4)
            )
        )
        coordinator.async_sync_grocer = AsyncMock()
        coordinator._async_persist = AsyncMock()
        await coordinator._handle_refresh_interval()
        coordinator.async_sync_grocer.assert_awaited_once()
        coordinator._async_persist.assert_awaited_once()
        assert coordinator.snapshot.quotes["milk"].price == 4
        assert not coordinator._force_full

    asyncio.run(run())


@pytest.mark.parametrize(
    "age, expected", [(60, True), (12 * 3600, False), (-60, False)]
)
def test_history_freshness_rejects_expired_and_future_mtime(
    tmp_path, monkeypatch, age, expected
):
    monkeypatch.setattr("smart_shopping_route.grocer.time.time", lambda: 1_000_000)
    target = tmp_path / "history_1.parquet"
    target.write_bytes(b"mirror")
    stamp = 1_000_000 - age
    os.utime(target, (stamp, stamp))
    assert GrocerData._history_fresh(target) is expected


def test_expired_history_is_refreshed_but_last_good_survives_outage(tmp_path):
    class Hass:
        config = SimpleNamespace(path=lambda _: str(tmp_path))

        async def async_add_executor_job(self, fn, *args):
            return fn(*args)

    data = GrocerData(Hass())
    target = data._dir / "history_1.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"old mirror")
    os.utime(target, (0, 0))
    data._async_download = AsyncMock(return_value=10)
    assert asyncio.run(data.async_sync_history(1))
    data._async_download.assert_awaited_once()
    data._async_download = AsyncMock(side_effect=GrocerError("offline"))
    assert asyncio.run(data.async_sync_history(1))
    assert target.read_bytes() == b"old mirror"


@pytest.mark.parametrize(
    "error", [GrocerError("cannot trim mirror"), OSError("permission denied")]
)
def test_mirror_maintenance_failure_never_skips_retail_prices(monkeypatch, error):
    async def run():
        coordinator, _ = make_price_coordinator(
            monkeypatch, ["Milk"], {"Milk": PriceQuote("paknsave", "1", "Milk 2L", 4)}
        )
        coordinator.hass.loop = asyncio.get_running_loop()
        coordinator.hass.is_stopping = False

        async def executor(fn, *args):
            return fn(*args)

        coordinator.hass.async_add_executor_job = executor
        coordinator.grocer_store_id = 228
        coordinator._grocer_data = SimpleNamespace(
            async_sync_names=AsyncMock(return_value=True),
            async_sync_store_prices=AsyncMock(return_value=True),
            async_mirrored_bytes=AsyncMock(return_value=0),
            async_match_with_history=AsyncMock(return_value=None),
            prune_history=Mock(side_effect=error),
        )
        await coordinator.async_refresh()
        assert coordinator.snapshot.quotes["milk"].price == 4
        assert coordinator.snapshot.status == "current"
        assert "milk" in coordinator._price_store.saved[-1]["history"]

    asyncio.run(run())


def test_item_added_while_pricing_is_queued(monkeypatch):
    async def run():
        coordinator = make_coordinator([item("1", "Milk")], monkeypatch)
        started, release = asyncio.Event(), asyncio.Event()
        captured = []

        async def refresh(*, force=False):
            captured.append([i.summary for i in coordinator.items])
            started.set()
            await release.wait()

        coordinator.prices.enabled = True
        coordinator.prices.async_refresh = refresh
        coordinator._request_prices()
        await started.wait()
        coordinator._source_updated([item("1", "Milk"), item("2", "Bread")])
        release.set()
        await asyncio.gather(*list(coordinator._tasks))
        assert any("Bread" in names for names in captured)
        assert len(captured) == 2

    asyncio.run(run())
