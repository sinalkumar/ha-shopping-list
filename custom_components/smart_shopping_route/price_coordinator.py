"""Price tracking for one route profile.

Prices refresh on their own slow interval and are completely independent of the
push-driven route: a slow or failing retailer must never delay the shopping
list. Each summary is priced once and reused, so adding one item does not
re-price the whole list.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
    """Quotes for the current list, plus a small price history."""

    quotes: dict[str, PriceQuote] = field(default_factory=dict)
    cheapest_seen: dict[str, float] = field(default_factory=dict)
    previous: dict[str, float] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)

    @property
    def total(self) -> float:
        return round(sum(quote.price for quote in self.quotes.values()), 2)

    @property
    def specials(self) -> list[PriceQuote]:
        return [quote for quote in self.quotes.values() if quote.is_special]

    @property
    def savings(self) -> float:
        return round(sum(quote.savings for quote in self.quotes.values()), 2)


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
        """Create the retailer client.

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

    async def _async_update_data(self) -> PriceSnapshot:
        if self._client is None:
            return self._snapshot

        wanted: dict[str, str] = {}
        for summary in self._summaries():
            key = normalize_summary(summary)
            if not key or key in self._snapshot.quotes:
                continue
            now = time.monotonic()
            if now < self._backoff_until.get(key, 0.0):
                continue
            wanted.setdefault(key, summary)

        if not wanted:
            return self._snapshot

        results = await asyncio.gather(
            *(self._async_price_one(key, summary) for key, summary in wanted.items()),
            return_exceptions=True,
        )
        failures = sum(1 for result in results if isinstance(result, BaseException))
        if failures and failures == len(results):
            raise UpdateFailed("No prices could be retrieved")

        _LOGGER.debug(
            "Priced %s item(s), %s failure(s)", len(results) - failures, failures
        )
        return self._snapshot

    async def _async_price_one(self, key: str, summary: str) -> None:
        assert self._client is not None
        try:
            async with self._semaphore:
                quote = await self._client.async_lookup(summary)
        except PriceError:
            self._schedule_retry(key)
            return
        self._attempts.pop(key, None)
        self._backoff_until.pop(key, None)
        if quote is None:
            # No confident match: remember nothing so a later rename or a new
            # product can be picked up, but do not retry every cycle either.
            self._snapshot.failed.add(key)
            self._schedule_retry(key)
            return
        self._snapshot.failed.discard(key)
        self._snapshot.quotes[key] = quote
        self._record_history(key, quote.price)

    def _record_history(self, key: str, price: float) -> None:
        previous = self._snapshot.quotes.get(key)
        if previous is not None:
            self._snapshot.previous[key] = previous.price
        cheapest = self._snapshot.cheapest_seen.get(key)
        if cheapest is None or price < cheapest:
            self._snapshot.cheapest_seen[key] = price

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
            "priced_items": len(self._snapshot.quotes),
            "unmatched_items": len(self._snapshot.failed),
            "scheduled_retries": len(self._backoff_until),
        }
