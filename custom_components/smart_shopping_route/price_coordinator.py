"""Price tracking for one route profile.

Prices refresh on their own slow interval and are completely independent of the
push-driven route: a slow or failing retailer must never delay the shopping
list. Each summary is priced once and then cached, so adding one item does not
re-price the whole list.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_GROCER_STORE_ID,
    CONF_PRICE_CHAIN,
    CONF_PRICE_STORE_ID,
    DOMAIN,
    GROCER_HISTORY_FILE_MAX,
    GROCER_STORE_REFRESH_HOURS,
    PRICE_CONCURRENCY,
    PRICE_FAILURE_BACKOFF_SECONDS,
    PRICE_HISTORY_MAX,
    PRICE_HISTORY_MAX_POINTS,
    PRICE_HISTORY_MIN_INTERVAL,
    PRICE_PIN_MAX,
    PRICE_REFRESH_HOURS,
)
from .grocer import GrocerData, GrocerError
from .models import normalize_summary
from .prices import PriceClient, PriceError, PriceQuote, ProductPin, create_client
from .storage import PriceStore

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PricePoint:
    """One observation of an item's price."""

    ts: float
    price: float


@dataclass(slots=True)
class PriceSnapshot:
    """Cached quotes for the current list, plus a per-item price series."""

    quotes: dict[str, PriceQuote] = field(default_factory=dict)
    # A bounded series per item. This is the only reference price available for
    # a chain that publishes no was-price, so it is what makes a drop or a
    # "cheapest so far" claim possible at all.
    history: dict[str, list[PricePoint]] = field(default_factory=dict)
    # Drops are an event of the refresh that observed them, so a price that
    # stays low is not reported as falling again on every later refresh.
    drops_now: dict[str, float] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)
    active: dict[str, int] = field(default_factory=dict)

    def points_for(self, key: str) -> list[PricePoint]:
        return self.history.get(key, [])

    def lowest_for(self, key: str) -> float | None:
        points = self.points_for(key)
        return min(point.price for point in points) if points else None

    def highest_for(self, key: str) -> float | None:
        points = self.points_for(key)
        return max(point.price for point in points) if points else None

    def reference_price(self, key: str) -> float | None:
        """The most recent earlier price that was higher than the current one.

        This is what a "was" figure means when the retailer supplies none: a
        price we actually observed, not an inferred or invented one.
        """

        quote = self.quotes.get(key)
        if quote is None:
            return None
        for point in reversed(self.points_for(key)):
            if point.price > quote.price:
                return point.price
        return None

    def is_at_lowest(self, key: str) -> bool:
        lowest = self.lowest_for(key)
        quote = self.quotes.get(key)
        if lowest is None or quote is None:
            return False
        return quote.price <= lowest

    def _active_quotes(self) -> list[tuple[str, int, PriceQuote]]:
        """Yield one entry per listed item, honouring duplicate summaries."""

        return [
            (key, count, self.quotes[key])
            for key, count in self.active.items()
            if key in self.quotes
        ]

    @property
    def total(self) -> float:
        """Sum priced items, counting duplicates at their real quantity."""

        return round(
            sum(quote.price * count for _key, count, quote in self._active_quotes()), 2
        )

    @property
    def specials(self) -> list[tuple[str, PriceQuote]]:
        return [
            (key, quote)
            for key, _count, quote in self._active_quotes()
            if quote.is_special
        ]

    @property
    def savings(self) -> float:
        return round(
            sum(quote.savings * count for _key, count, quote in self._active_quotes()),
            2,
        )

    def drop_for(self, key: str) -> float | None:
        """Return how far the price fell in the last refresh, if it did.

        This is the only specials signal available for a chain that publishes no
        was-price, because the comparison comes from our own history.
        """

        return self.drops_now.get(key)

    @property
    def drops(self) -> list[tuple[str, PriceQuote, float]]:
        return [
            (key, quote, drop)
            for key, drop in self.drops_now.items()
            if (quote := self.quotes.get(key)) is not None and not quote.is_special
        ]

    @property
    def priced_count(self) -> int:
        return sum(count for _key, count, _quote in self._active_quotes())

    @property
    def unmatched_count(self) -> int:
        return sum(count for key, count in self.active.items() if key in self.failed)


def _parse_point(raw: Any) -> PricePoint | None:
    """Build a point from persisted data, or None if it is unusable.

    Persisted history is user data that an older version or a partial write can
    leave malformed, and it is read during setup, so a bad point must never
    raise: one corrupt entry would otherwise stop the profile loading.
    """

    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    try:
        ts = float(raw[0])
        price = float(raw[1])
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(ts) and math.isfinite(price)) or price <= 0:
        return None
    return PricePoint(ts, price)


class PriceCoordinator(DataUpdateCoordinator[PriceSnapshot]):
    """Look up one price per shopping item, slowly and politely."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        summaries: Any,
    ) -> None:
        self.entry = entry
        self.chain = entry.data.get(CONF_PRICE_CHAIN)
        self._summaries = summaries
        self._client: PriceClient | None = None
        self._snapshot = PriceSnapshot()
        self._attempts: dict[str, int] = {}
        self._backoff_until: dict[str, float] = {}
        self._semaphore = asyncio.Semaphore(PRICE_CONCURRENCY)
        self._update_task: asyncio.Task[Any] | None = None
        self._force_full = False
        self._rerun_requested = False
        self._closed = False
        self._pins: dict[str, ProductPin] = {}
        self._on_update: Callable[[], None] | None = None
        self._grocer_data: GrocerData | None = None
        self._hass_ref = hass
        self.grocer_store_id: int | None = entry.data.get(CONF_GROCER_STORE_ID)
        # Products whose history has already been imported, so a backfill runs
        # once per product rather than on every refresh.
        self._backfilled: set[str] = set()
        self._backfill_attempts: set[str] = set()
        self._matched_products: set[int] = set()
        self._last_store_sync = 0.0
        self._mirror_bytes: int | None = None
        self._price_store = PriceStore(hass, entry.entry_id)
        self._unsub_listener: CALLBACK_TYPE | None = None
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} prices {entry.title}",
            update_interval=timedelta(hours=PRICE_REFRESH_HOURS),
            # Passing the entry explicitly is the supported pattern and avoids
            # the ContextVar fallback the framework reports as incorrect usage.
            config_entry=entry,
        )

    def async_set_update_callback(self, on_update: Callable[[], None]) -> None:
        """Register the callback entities use to re-render after an update."""

        self._on_update = on_update

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def history_enabled(self) -> bool:
        """grocer.nz history is available for this profile."""

        return self.grocer_store_id is not None and GrocerData.duckdb_available()

    @property
    def grocer(self) -> GrocerData:
        """Created lazily: it needs a config dir, and most paths never use it."""

        if self._grocer_data is None:
            self._grocer_data = GrocerData(self._hass_ref)
        return self._grocer_data

    async def async_sync_grocer(self, *, force: bool = False) -> None:
        """Mirror the catalogue and this store's prices, tolerating failure.

        Refreshed on its own slow schedule rather than every time, so a restart
        does not re-download it. A failure leaves the existing mirror in place,
        which is the point of keeping one.
        """

        if not self.history_enabled or self.grocer_store_id is None:
            return
        now = time.time()
        try:
            if (
                force
                or now - self._last_store_sync >= GROCER_STORE_REFRESH_HOURS * 3600
            ):
                # Names rarely change, so they follow the store refresh.
                await self.grocer.async_sync_names()
                if await self.grocer.async_sync_store_prices(self.grocer_store_id):
                    self._last_store_sync = now
        except GrocerError as err:
            _LOGGER.debug("grocer.nz sync failed: %s", err)
        if self.history_enabled:
            # Persist the sync timestamps, otherwise a profile that has not
            # matched anything yet re-downloads on every restart.
            await self._async_persist()
            self._mirror_bytes = await self.grocer.async_mirrored_bytes()
            # Bound the mirror so history is kept without growing forever.
            keep = await self.hass.async_add_executor_job(self._retained_products)
            trimmed = await self.hass.async_add_executor_job(
                self.grocer.prune_history, keep, GROCER_HISTORY_FILE_MAX
            )
            if trimmed:
                _LOGGER.debug("Trimmed %s mirrored history file(s)", trimmed)

    def _retained_products(self) -> set[int]:
        """Products still worth keeping history for: those on the current list."""

        return set(self._matched_products)

    async def _async_backfill(self, key: str, quote: PriceQuote) -> None:
        """Import multi-year history for a validated grocer.nz match.

        The match is validated against the price the retailer just quoted, so an
        import cannot attach another product's history.
        """

        if (
            not self.history_enabled
            or self.grocer_store_id is None
            or key in self._backfilled
            or key in self._backfill_attempts
        ):
            return
        # Tracked separately from _backfilled: a transient failure must be
        # retried, while a definitive "no match" or a successful import should
        # not be attempted again.
        pin = self._pins.get(key)
        if pin is not None and pin.grocer_product_id:
            # An explicit choice: use exactly that product's history.
            try:
                match = await self.grocer.async_pinned_match(
                    self.grocer_store_id, pin.grocer_product_id
                )
            except GrocerError as err:
                _LOGGER.debug("grocer.nz pinned history failed: %s", err)
                match = None
            finally:
                self._backfill_attempts.discard(key)
            if match is not None and match.history:
                self._apply_history(key, match)
            return
        self._backfill_attempts.add(key)
        try:
            match = await self.grocer.async_match_with_history(
                self.grocer_store_id, quote.product_name, quote.price
            )
        except GrocerError as err:
            _LOGGER.debug("grocer.nz match failed: %s", err)
            return
        finally:
            self._backfill_attempts.discard(key)
        if match is None:
            # Definitive: this product is not in the catalogue, so stop asking.
            self._backfilled.add(key)
            return
        if not match.history:
            return
        self._apply_history(key, match)
        self._backfilled.add(key)

    def _apply_history(self, key: str, match: Any) -> None:
        """Merge imported history with our own fresher observations."""

        imported = [
            PricePoint(ts, price)
            for ts, price in match.history[-PRICE_HISTORY_MAX_POINTS:]
        ]
        existing = self._snapshot.history.get(key, [])
        if existing:
            # Keep our own observations too: they are fresher than the mirror.
            merged = {round(point.ts): point for point in imported}
            merged.update({round(point.ts): point for point in existing})
            combined = [merged[ts] for ts in sorted(merged)]
        else:
            combined = imported
        self._snapshot.history[key] = combined[-PRICE_HISTORY_MAX_POINTS:]
        self._matched_products.add(match.product.product_id)
        self._backfilled.add(key)
        _LOGGER.debug(
            "Imported %s historical price point(s) for one item", len(imported)
        )

    @property
    def _chain_key(self) -> str:
        """Chain identity used for pin comparison, normalised once."""

        return self.chain or ""

    def async_setup(self) -> None:
        """Create the retailer client and start the refresh interval.

        Deliberately network-free: entry setup must not wait on a retailer, so
        the first price reading is triggered separately in the background.
        """

        try:
            self._client = create_client(
                self.hass, self.chain, self.entry.data.get(CONF_PRICE_STORE_ID)
            )
        except PriceError as err:
            _LOGGER.warning("Price client unavailable for this profile: %s", err)
            self._client = None
            return
        if self._client is None:
            return
        # DataUpdateCoordinator only schedules its interval once it has a
        # listener, so without this the 12-hour refresh would never run. The
        # listener also forwards every completed update to entities: the
        # interval updates the snapshot without going through
        # RouteCoordinator._async_price_then_notify, so without this the price
        # sensors and item descriptions would keep showing stale prices.
        if self._unsub_listener is None:
            self._unsub_listener = self.async_add_listener(
                self._async_handle_update_done
            )

    @callback
    def _async_handle_update_done(self) -> None:
        if self._closed or self._on_update is None:
            return
        self._on_update()

    async def async_load(self) -> None:
        """Restore pins and price history before the first reading."""

        data = await self._price_store.async_load()
        pins = data.get("pins") if isinstance(data.get("pins"), dict) else {}
        self._pins = {}
        for key, value in pins.items():
            if not isinstance(value, dict):
                continue
            # Pins are per retailer: a product ID or search phrase pinned at
            # Woolworths is meaningless at Pak'nSave, and reusing it after a
            # reconfigure would price the wrong product. The stored chain must
            # be compared as persisted, before it is overwritten with the
            # profile's current chain.
            if (value.get("chain") or "") != self._chain_key:
                _LOGGER.debug("Dropping a pin recorded for a different chain")
                continue
            self._pins[key] = ProductPin.from_dict(self._chain_key, value)
        grocer = data.get("grocer") if isinstance(data.get("grocer"), dict) else {}
        for attr, key in (("_last_store_sync", "store_at"),):
            value = grocer.get(key)
            if isinstance(value, (int, float)):
                setattr(self, attr, float(value))
        history = data.get("history")
        if isinstance(history, dict):
            for key, entry in history.items():
                points = entry.get("points") if isinstance(entry, dict) else None
                if not isinstance(points, list):
                    continue
                series: list[PricePoint] = []
                for point in points:
                    parsed = _parse_point(point)
                    if parsed is not None:
                        series.append(parsed)
                if series:
                    self._snapshot.history[str(key)] = series[
                        -PRICE_HISTORY_MAX_POINTS:
                    ]
        # A restored store can predate a lower bound, and a failed or disabled
        # refresh would never reach the trim in _prune, so bound it here too.
        self._trim_history(set(self._snapshot.history))

    async def _async_persist(self) -> None:
        self._trim_history(set(self._snapshot.history))
        await self._price_store.async_save({
            "pins": {key: pin.as_dict() for key, pin in self._pins.items()},
            "history": {
                key: {"points": [[point.ts, point.price] for point in series]}
                for key, series in self._snapshot.history.items()
            },
            "grocer": {"store_at": self._last_store_sync},
        })

    async def async_pin(
        self,
        summary: str,
        *,
        query: str = "",
        product_id: str | None = None,
        product_name: str | None = None,
        grocer_product_id: int | None = None,
    ) -> None:
        """Pin the product an item means, then re-price just that item."""

        key = normalize_summary(summary)
        if not key:
            raise ValueError("Item has no summary to pin")
        self._trim_pins()
        self._pins[key] = ProductPin(
            chain=self._chain_key,
            query=query.strip(),
            product_id=product_id,
            product_name=product_name,
            grocer_product_id=grocer_product_id,
        )
        # Forget what we concluded before; the pin replaces it.
        self._snapshot.quotes.pop(key, None)
        self._snapshot.failed.discard(key)
        self._backoff_until.pop(key, None)
        self._attempts.pop(key, None)
        await self._async_persist()
        self._notify_if_refresh_deferred()
        await self.async_refresh()

    async def async_unpin(self, summary: str) -> None:
        key = normalize_summary(summary)
        if self._pins.pop(key, None) is None:
            return
        self._snapshot.quotes.pop(key, None)
        self._snapshot.failed.discard(key)
        self._backoff_until.pop(key, None)
        self._attempts.pop(key, None)
        await self._async_persist()
        self._notify_if_refresh_deferred()
        await self.async_refresh()

    @callback
    def _notify_if_refresh_deferred(self) -> None:
        """Notify now only when the refresh that follows cannot run yet.

        A refresh notifies through the update callback, so notifying here as
        well would write every price-aware entity's state twice. When an update
        is already in flight the refresh is deferred instead, and without this
        a cleared quote would stay on screen until that request finished.
        """

        if self._update_task is not None and not self._update_task.done():
            self._async_handle_update_done()

    def _trim_pins(self) -> None:
        """Bound pins, dropping the oldest-inserted ones beyond the ceiling."""

        excess = len(self._pins) + 1 - PRICE_PIN_MAX
        if excess <= 0:
            return
        for key in list(self._pins)[:excess]:
            self._pins.pop(key, None)

    def pin_for(self, summary: str) -> ProductPin | None:
        return self._pins.get(normalize_summary(summary))

    async def async_shutdown(self) -> None:
        """Stop refreshing and make sure no update outlives the profile."""

        self._closed = True
        if self._unsub_listener is not None:
            self._unsub_listener()
            self._unsub_listener = None
        await super().async_shutdown()
        # DataUpdateCoordinator cancels scheduled refreshes but not a request
        # already in flight, so await it here; otherwise a slow retailer could
        # mutate the snapshot after the profile was unloaded.
        task = self._update_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._update_task = None
        if self._pins or self._snapshot.history:
            await self._async_persist()

    async def async_refresh(self, *, force: bool = True) -> None:  # type: ignore[override]
        """Refresh under a tracked task so shutdown can cancel it.

        ``force`` re-prices items that already have a quote. The interval and
        manual refreshes force, so prices actually change; source pushes do not,
        so adding one item stays one request.
        """

        if self._closed or self._client is None:
            return
        if force:
            self._force_full = True
        if self._update_task is not None and not self._update_task.done():
            # One refresh at a time: the retailer is not a queue. Remember that
            # another was wanted so a pin change or a newly added item is not
            # silently left until the next interval.
            self._rerun_requested = True
            return
        self._rerun_requested = False
        self._update_task = asyncio.create_task(self._async_refresh_guarded())
        try:
            await self._update_task
        except asyncio.CancelledError:
            raise
        finally:
            if self._update_task is not None and self._update_task.done():
                self._update_task = None
        if self._rerun_requested and not self._closed:
            self._rerun_requested = False
            await self.async_refresh(force=False)

    async def _async_refresh_guarded(self) -> None:
        await self.async_sync_grocer()
        try:
            await super().async_refresh()
        except UpdateFailed:
            _LOGGER.debug("Price refresh failed; keeping previous quotes")
            return
        # History is the only specials signal for a chain with no was-price, and
        # Home Assistant shutdown does not unload entries, so it must be written
        # when it changes rather than only on unload.
        if self._snapshot.history or self._pins:
            await self._async_persist()

    async def _handle_refresh_interval(self, _now: Any = None) -> None:
        """Re-price everything on the slow interval, not just new items."""

        self._force_full = True
        await super()._handle_refresh_interval(_now)

    async def _async_update_data(self) -> PriceSnapshot:
        if self._client is None or self._closed:
            return self._snapshot

        now = time.monotonic()
        # One representative summary per normalized key, plus the real
        # multiplicity so duplicate items count as separate purchases.
        first_summary: dict[str, str] = {}
        active: Counter[str] = Counter()
        for summary in self._summaries():
            key = normalize_summary(summary)
            if not key:
                continue
            active[key] += 1
            first_summary.setdefault(key, summary)
        self._snapshot.active = dict(active)

        force = self._force_full
        self._force_full = False
        to_fetch = {
            key: summary
            for key, summary in first_summary.items()
            if (force or key not in self._snapshot.quotes)
            and (force or key not in self._snapshot.failed)
            and now >= self._backoff_until.get(key, 0.0)
        }

        if to_fetch:
            results = await asyncio.gather(
                *(
                    self._async_price_one(key, summary)
                    for key, summary in to_fetch.items()
                ),
                return_exceptions=True,
            )
            transport_failures = sum(
                1 for result in results if isinstance(result, PriceError)
            )
            unexpected = [
                result
                for result in results
                if isinstance(result, BaseException)
                and not isinstance(result, PriceError)
            ]
            if unexpected:
                # One line per cycle, not per item: a retailer changing its
                # response shape must not emit a warning for every item on
                # every refresh.
                _LOGGER.warning(
                    "Price lookup failed unexpectedly for %s item(s): %s",
                    len(unexpected),
                    ", ".join(sorted({type(err).__name__ for err in unexpected})),
                )
            if transport_failures and transport_failures == len(results):
                # Every request failed: report a real failure so Home Assistant
                # shows the entity as degraded instead of silently showing stale
                # or missing prices.
                raise UpdateFailed("No prices could be retrieved")
            _LOGGER.debug(
                "Priced %s item(s), %s transport failure(s)",
                len(results) - transport_failures,
                transport_failures,
            )

        self._prune()
        return self._snapshot

    def _prune(self) -> None:
        """Drop cache entries for items that are no longer on the list."""

        active = set(self._snapshot.active)
        for key in list(self._snapshot.quotes):
            if key not in active:
                self._snapshot.quotes.pop(key, None)
        for key in list(self._snapshot.failed):
            if key not in active:
                self._snapshot.failed.discard(key)
        for key in list(self._backoff_until):
            if key not in active:
                self._backoff_until.pop(key, None)
                self._attempts.pop(key, None)
        # Pins deliberately outlive their item: a household shorthand recurs,
        # and a user decision should not evaporate when an item is ticked off.
        self._trim_history(active)

    def _trim_history(self, active: set[str]) -> None:
        """Bound the persisted series count, dropping off-list items first."""

        history = self._snapshot.history
        excess = len(history) - PRICE_HISTORY_MAX
        if excess <= 0:
            return
        for key in [key for key in history if key not in active][:excess]:
            history.pop(key, None)
        excess = len(history) - PRICE_HISTORY_MAX
        for key in list(history)[: max(0, excess)]:
            history.pop(key, None)

    async def _async_price_one(self, key: str, summary: str) -> None:
        assert self._client is not None
        try:
            async with self._semaphore:
                quote = await self._client.async_lookup(summary, self._pins.get(key))
        except PriceError:
            self._schedule_retry(key)
            raise
        except Exception:
            # An unexpected error (a changed response shape, say) still needs
            # backoff, otherwise every source push retries it.
            self._schedule_retry(key)
            raise
        if self._closed:
            return
        self._attempts.pop(key, None)
        self._backoff_until.pop(key, None)
        if quote is None:
            # No confident match. Remember that so the item is not searched on
            # every cycle, but keep it retryable in case the item is renamed.
            self._snapshot.failed.add(key)
            self._schedule_retry(key)
            return
        self._snapshot.failed.discard(key)
        self._record_history(key, quote)
        self._snapshot.quotes[key] = quote
        await self._async_backfill(key, quote)

    def _record_history(self, key: str, quote: PriceQuote) -> None:
        """Append an observation, then derive the drop from the series.

        The series is recorded before the quote is replaced, so the reference
        price is an earlier observation rather than the value being stored.
        """

        series = self._snapshot.history.setdefault(key, [])
        now = time.time()
        last = series[-1] if series else None

        # Derive the drop from the observation before this one. The new price is
        # appended afterwards, so the comparison cannot be against itself.
        # Only the keys observed in this cycle are touched, so a drop found
        # earlier survives an incremental refresh that did not re-price it.
        if last is not None and last.price > quote.price and not quote.is_special:
            self._snapshot.drops_now[key] = round(last.price - quote.price, 2)
        else:
            self._snapshot.drops_now.pop(key, None)

        if (
            last is None
            or last.price != quote.price
            or now - last.ts >= PRICE_HISTORY_MIN_INTERVAL
        ):
            series.append(PricePoint(now, quote.price))
            if len(series) > PRICE_HISTORY_MAX_POINTS:
                del series[: len(series) - PRICE_HISTORY_MAX_POINTS]

    def _schedule_retry(self, key: str) -> None:
        attempts = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempts
        self._backoff_until[key] = (
            time.monotonic() + PRICE_FAILURE_BACKOFF_SECONDS * attempts
        )

    def quote_for(self, summary: str) -> PriceQuote | None:
        return self._snapshot.quotes.get(normalize_summary(summary))

    def cheapest_seen_for(self, summary: str) -> float | None:
        return self._snapshot.lowest_for(normalize_summary(summary))

    @property
    def snapshot(self) -> PriceSnapshot:
        return self._snapshot

    def as_diagnostics(self) -> dict[str, Any]:
        """Return counts only; product names and item text stay out of diagnostics."""

        return {
            "chain": self.chain,
            "enabled": self.enabled,
            "priced_items": self._snapshot.priced_count,
            "unmatched_items": self._snapshot.unmatched_count,
            "scheduled_retries": len(self._backoff_until),
            "pinned_items": len(self._pins),
            "items_with_history": len(self._snapshot.history),
            "grocer_history": self.history_enabled,
            "grocer_mirror_bytes": (
                self._mirror_bytes if self._mirror_bytes is not None else 0
            ),
        }
