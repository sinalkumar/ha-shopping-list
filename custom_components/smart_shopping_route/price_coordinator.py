"""Price tracking for one route profile.

Prices refresh on their own slow interval and are completely independent of the
push-driven route: a slow or failing retailer must never delay the shopping
list. Each summary is priced once and then cached, so adding one item does not
re-price the whole list.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_PRICE_CHAIN,
    CONF_PRICE_STORE_ID,
    DOMAIN,
    PRICE_CONCURRENCY,
    PRICE_FAILURE_BACKOFF_SECONDS,
    PRICE_HISTORY_MAX,
    PRICE_REFRESH_HOURS,
)
from .models import normalize_summary
from .prices import PriceClient, PriceError, PriceQuote, ProductPin, create_client
from .storage import PriceStore

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PriceSnapshot:
    """Cached quotes for the current list, plus a small price history."""

    quotes: dict[str, PriceQuote] = field(default_factory=dict)
    cheapest_seen: dict[str, float] = field(default_factory=dict)
    previous: dict[str, float] = field(default_factory=dict)
    # Drops are an event of the refresh that observed them, so a price that
    # stays low is not reported as falling again on every later refresh.
    drops_now: dict[str, float] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)
    active: dict[str, int] = field(default_factory=dict)
    pinned: set[str] = field(default_factory=set)

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
        self._closed = False
        self._pins: dict[str, ProductPin] = {}
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

    @property
    def enabled(self) -> bool:
        return self._client is not None

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
        # listener, so without this the 12-hour refresh would never run.
        if self._unsub_listener is None:
            self._unsub_listener = self.async_add_listener(lambda: None)

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
        history = data.get("history")
        if isinstance(history, dict):
            for key, entry in history.items():
                if not isinstance(entry, dict):
                    continue
                cheapest = entry.get("cheapest")
                previous = entry.get("previous")
                if isinstance(cheapest, (int, float)):
                    self._snapshot.cheapest_seen[key] = float(cheapest)
                if isinstance(previous, (int, float)):
                    self._snapshot.previous[key] = float(previous)

    async def _async_persist(self) -> None:
        await self._price_store.async_save({
            "pins": {key: pin.as_dict() for key, pin in self._pins.items()},
            "history": {
                key: {
                    "cheapest": cheapest,
                    **(
                        {"previous": self._snapshot.previous[key]}
                        if key in self._snapshot.previous
                        else {}
                    ),
                }
                for key, cheapest in self._snapshot.cheapest_seen.items()
            },
        })

    async def async_pin(
        self,
        summary: str,
        *,
        query: str = "",
        product_id: str | None = None,
        product_name: str | None = None,
    ) -> None:
        """Pin the product an item means, then re-price just that item."""

        key = normalize_summary(summary)
        if not key:
            raise ValueError("Item has no summary to pin")
        self._pins[key] = ProductPin(
            chain=self._chain_key,
            query=query.strip(),
            product_id=product_id,
            product_name=product_name,
        )
        # Forget what we concluded before; the pin replaces it.
        self._snapshot.quotes.pop(key, None)
        self._snapshot.failed.discard(key)
        self._backoff_until.pop(key, None)
        self._attempts.pop(key, None)
        await self._async_persist()
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
        await self.async_refresh()

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
        if self._pins or self._snapshot.cheapest_seen:
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
            # One refresh at a time: the retailer is not a queue.
            return
        self._update_task = asyncio.create_task(self._async_refresh_guarded())
        try:
            await self._update_task
        except asyncio.CancelledError:
            raise
        finally:
            if self._update_task is not None and self._update_task.done():
                self._update_task = None

    async def _async_refresh_guarded(self) -> None:
        try:
            await super().async_refresh()
        except UpdateFailed:
            _LOGGER.debug("Price refresh failed; keeping previous quotes")
            return
        # History is the only specials signal for a chain with no was-price, and
        # Home Assistant shutdown does not unload entries, so it must be written
        # when it changes rather than only on unload.
        if self._snapshot.cheapest_seen or self._pins:
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
        self._snapshot.drops_now.clear()

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
        # Pins outlive their item on purpose: a household shorthand recurs, and
        # a user decision should not evaporate when the item is ticked off.
        self._snapshot.pinned &= active | set(self._pins)
        self._trim_history(active)

    def _trim_history(self, active: set[str]) -> None:
        """Bound persisted price history, dropping off-list entries first."""

        for store in (self._snapshot.cheapest_seen, self._snapshot.previous):
            excess = len(store) - PRICE_HISTORY_MAX
            if excess <= 0:
                continue
            for key in [key for key in store if key not in active][:excess]:
                store.pop(key, None)
            excess = len(store) - PRICE_HISTORY_MAX
            for key in list(store)[: max(0, excess)]:
                store.pop(key, None)

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
        if key in self._pins:
            self._snapshot.pinned.add(key)

    def _record_history(self, key: str, quote: PriceQuote) -> None:
        """Record history from the previously known quote, before replacing it."""

        previous = self._snapshot.quotes.get(key)
        if previous is not None and previous.price != quote.price:
            self._snapshot.previous[key] = previous.price
            if previous.price > quote.price and not quote.is_special:
                self._snapshot.drops_now[key] = round(previous.price - quote.price, 2)
            else:
                self._snapshot.drops_now.pop(key, None)
        cheapest = self._snapshot.cheapest_seen.get(key)
        if cheapest is None or quote.price < cheapest:
            self._snapshot.cheapest_seen[key] = quote.price

    def _schedule_retry(self, key: str) -> None:
        attempts = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempts
        self._backoff_until[key] = (
            time.monotonic() + PRICE_FAILURE_BACKOFF_SECONDS * attempts
        )

    def quote_for(self, summary: str) -> PriceQuote | None:
        return self._snapshot.quotes.get(normalize_summary(summary))

    def cheapest_seen_for(self, summary: str) -> float | None:
        return self._snapshot.cheapest_seen.get(normalize_summary(summary))

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
        }
