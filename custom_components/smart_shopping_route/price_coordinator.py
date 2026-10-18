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
    PRICE_REFRESH_HOURS,
)
from .models import normalize_summary
from .prices import PriceClient, PriceError, PriceQuote, create_client

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PriceSnapshot:
    """Cached quotes for the current list, plus a small price history."""

    quotes: dict[str, PriceQuote] = field(default_factory=dict)
    cheapest_seen: dict[str, float] = field(default_factory=dict)
    previous: dict[str, float] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)
    active: dict[str, int] = field(default_factory=dict)

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
            for result in unexpected:
                _LOGGER.warning(
                    "Price lookup raised unexpectedly: %s", type(result).__name__
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

    async def _async_price_one(self, key: str, summary: str) -> None:
        assert self._client is not None
        try:
            async with self._semaphore:
                quote = await self._client.async_lookup(summary)
        except PriceError:
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

    def _record_history(self, key: str, quote: PriceQuote) -> None:
        """Record history from the previously known quote, before replacing it."""

        previous = self._snapshot.quotes.get(key)
        if previous is not None and previous.price != quote.price:
            self._snapshot.previous[key] = previous.price
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
        }
