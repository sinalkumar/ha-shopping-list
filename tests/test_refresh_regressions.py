"""End-to-end coordinator lifecycle regressions, without production mutations."""

import asyncio
import datetime as dt
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import smart_shopping_route.price_coordinator as price_coordinator
from smart_shopping_route.const import (
    PRICE_WEEKLY_REFRESH_HOUR,
    PRICE_WEEKLY_REFRESH_WEEKDAY,
)
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


# --- weekly rollover --------------------------------------------------------
# Both chains publish the new week's prices in the early hours of Monday NZ
# time, so a 12-hour interval can be half a day late on exactly the morning the
# whole list changes price.

MONDAY_6AM = dt.datetime(2026, 10, 19, 6, 0, tzinfo=dt.UTC)


def test_weekly_rollover_is_scheduled_for_monday_six(monkeypatch):
    """A listener that is never registered cannot re-price anything."""

    captured: dict[str, object] = {}

    def fake_track(hass, action, hour=None, minute=None, second=None):
        captured.update(action=action, hour=hour, minute=minute, second=second)
        return lambda: None

    monkeypatch.setattr(price_coordinator, "create_client", lambda *_a, **_k: Mock())
    monkeypatch.setattr(price_coordinator, "async_track_time_change", fake_track)
    coordinator = PriceCoordinator(
        SimpleNamespace(loop=asyncio.new_event_loop()),
        fake_entry(),
        summaries=lambda: ["Milk"],
    )

    coordinator.async_setup()

    assert captured["action"] == coordinator._async_weekly_rollover
    assert captured["hour"] == PRICE_WEEKLY_REFRESH_HOUR == 6
    assert captured["minute"] == 0
    assert coordinator._unsub_weekly is not None


@pytest.mark.parametrize("weekday", [0, 1, 3, 6])
def test_weekly_rollover_runs_on_monday_only(monkeypatch, weekday):
    """The listener fires daily, so the weekday is what makes it weekly."""

    coordinator, calls = make_price_coordinator(
        monkeypatch, ["Milk"], {"Milk": PriceQuote("woolworths", "1", "Milk 2L", 4.0)}
    )
    coordinator.async_sync_grocer = AsyncMock()
    coordinator._async_persist = AsyncMock()

    asyncio.run(
        coordinator._async_weekly_rollover(MONDAY_6AM + dt.timedelta(days=weekday))
    )

    assert PRICE_WEEKLY_REFRESH_WEEKDAY == 0
    assert len(calls) == (1 if weekday == 0 else 0)
    if weekday == 0:
        # grocer.nz history is what makes a Pak'nSave special visible, so the
        # mirror must not wait for its own 12-hour timer.
        coordinator.async_sync_grocer.assert_awaited_once_with(force=True)
    else:
        coordinator.async_sync_grocer.assert_not_awaited()


def test_weekly_rollover_reprices_items_that_already_have_a_quote(monkeypatch):
    """A cached quote must not survive the week's price change."""

    coordinator, calls = make_price_coordinator(
        monkeypatch, ["Milk"], {"Milk": PriceQuote("woolworths", "1", "Milk 2L", 4.0)}
    )
    coordinator.async_sync_grocer = AsyncMock()
    coordinator._async_persist = AsyncMock()
    asyncio.run(coordinator._async_update_data())
    assert calls == ["Milk"]

    asyncio.run(coordinator._async_weekly_rollover(MONDAY_6AM))

    assert calls == ["Milk", "Milk"]
    coordinator.async_sync_grocer.assert_awaited_once_with(force=True)


def test_weekly_rollover_stops_with_the_profile(monkeypatch):
    """An unloaded profile must not keep a listener alive."""

    monkeypatch.setattr(
        price_coordinator, "async_track_time_change", lambda *a, **k: lambda: None
    )
    monkeypatch.setattr(price_coordinator, "create_client", lambda *_a, **_k: Mock())
    coordinator = PriceCoordinator(
        SimpleNamespace(loop=asyncio.new_event_loop()),
        fake_entry(),
        summaries=lambda: ["Milk"],
    )
    coordinator.async_setup()
    assert coordinator._unsub_weekly is not None

    asyncio.run(coordinator.async_shutdown())

    assert coordinator._unsub_weekly is None
