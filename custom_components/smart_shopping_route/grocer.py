"""grocer.nz price data: catalogue, per-store prices, and multi-year history.

grocer.nz publishes its dataset as public, unauthenticated assets, which is the
only way to get real price history for New Zealand supermarkets. Foodstuffs in
particular publishes no was-price at all, so without history there is no way to
tell a Pak'nSave special from a normal price.

Everything downloaded here is mirrored to disk so the integration keeps working
when grocer.nz is unreachable, and so the history we paid to download is not
thrown away on a restart.

Two things this deliberately does not do:

* It is not the primary source of *current* prices. The retailers' own endpoints
  are fresher and rank search results by relevance, which matters because a
  flat catalogue scan is much worse at guessing what "Strawberries" means.
* It does not trust a name match on its own. A candidate is accepted only when
  its price agrees with what the retailer just quoted, which makes the match
  self-validating.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import GROCER_MIRROR_DIR
from .parquet import ParquetError, read_columns

_LOGGER = logging.getLogger(__name__)

ASSET_BASE = "https://assets-prod.grocer.nz/public"
STORE_PRICES_URL = f"{ASSET_BASE}/prices_per_store_v3/public_prices_{{store}}.parquet"
PRODUCT_HISTORY_URL = f"{ASSET_BASE}/price_history_v3/price_history_{{product}}.parquet"

# Product names come from grocer.nz's own search service rather than the DuckDB
# catalogue: it is a plain HTTP call with no compiled dependency, and it ranks
# results, which matters because a flat catalogue scan is much worse at guessing
# what "Strawberries" means. This is the public search key the web app ships.
SEARCH_URL = "https://meilisearch.grocer.nz/indexes/products/search"
SEARCH_KEY = "7f58239330307ec585c86863f985ab83cbb9ce951a9601c66e158548fb632fd1"
# A price match must be within this many cents to count as the same product.
PRICE_TOLERANCE = 0.02
# Minimum name similarity before a candidate is even considered.
MIN_NAME_RATIO = 0.55
# Pack sizes must agree, because two sizes of the same product often share a
# price and price agreement alone would then accept the wrong one.
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|g|ml|l|pk|pack|sheets|s)\b", re.I)


def _sizes(text: str) -> set[str]:
    """Extract comparable pack sizes, normalised to a base unit."""

    found: set[str] = set()
    for amount, unit in _SIZE_RE.findall(str(text)):
        unit = unit.lower()
        try:
            value = float(amount)
        except ValueError:
            continue
        if unit == "kg":
            value, unit = value * 1000, "g"
        elif unit == "l":
            value, unit = value * 1000, "ml"
        elif unit in ("pack", "pk"):
            unit = "pk"
        found.add(f"{value:g}{unit}")
    return found


def _sizes_compatible(wanted: str, candidate: str) -> bool:
    """Reject a candidate whose pack size contradicts the retailer's product."""

    want = _sizes(wanted)
    if not want:
        return True
    have = _sizes(candidate)
    if not have:
        return True
    return bool(want & have)


MAX_HISTORY_POINTS = 400


class GrocerError(Exception):
    """Raised when grocer.nz data cannot be read."""


@dataclass(slots=True)
class GrocerPrice:
    """One row of a store's price file."""

    product_id: int
    regular: float
    sale: float | None
    updated_at: str | None = None

    @property
    def effective(self) -> float:
        return self.sale if self.sale is not None else self.regular


@dataclass(slots=True)
class GrocerProduct:
    """A catalogue entry."""

    product_id: int
    name: str
    brand: str = ""
    size: str = ""
    stores: set[int] | None = None

    @property
    def label(self) -> str:
        return " ".join(part for part in (self.brand, self.name, self.size) if part)


@dataclass(slots=True)
class GrocerMatch:
    """A validated product match plus its history."""

    product: GrocerProduct
    price: GrocerPrice
    history: list[tuple[float, float]] = field(default_factory=list)
    name_ratio: float = 0.0

    def history_for(self, limit: int = MAX_HISTORY_POINTS) -> list[tuple[float, float]]:
        return self.history[-limit:]


def _normalise(text: str) -> str:
    """Fold text for comparison.

    Apostrophes and quotes are removed rather than replaced with a space, so
    "PAK'nSAVE" and "PakNSave" both become "paknsave". Other punctuation becomes
    a space, which keeps words that were separated by it comparable.
    """

    value = "" if text is None else str(text)
    if value.lower() == "nan":
        value = ""
    value = re.sub(r"['\u2019\u2018`\"]", "", value.lower())
    return re.sub(r"[^a-z0-9 ]+", " ", value).strip() and " ".join(
        re.sub(r"[^a-z0-9 ]+", " ", value).split()
    )


class GrocerData:
    """Local mirror of the grocer.nz assets, with on-demand syncing."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._dir = Path(hass.config.path(GROCER_MIRROR_DIR))
        self._session: aiohttp.ClientSession | None = None
        self._catalogue: dict[int, GrocerProduct] | None = None
        self._stores: dict[int, str] | None = None
        self._store_prices: dict[int, dict[int, GrocerPrice]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Parquet reads run in executor threads and are pure Python, so they are
        # safe to run concurrently; no shared database handle exists any more.
        self._mirror_bytes: int | None = None

    # -- infrastructure ---------------------------------------------------

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = async_get_clientsession(self.hass)
        return self._session

    def _lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    @staticmethod
    def duckdb_available() -> bool:
        """Kept for callers; reading no longer needs any third-party package."""

        return True

    async def _async_search(self, query: str) -> list[GrocerProduct]:
        """Ask grocer.nz's search service for products matching a phrase.

        This replaces reading the DuckDB catalogue, which needed a compiled
        dependency with no Home Assistant OS wheels. Search also ranks results by
        popularity, which a flat catalogue scan cannot do.
        """

        if not query.strip():
            return []
        payload = {"q": query, "limit": 20}
        try:
            async with self.session.post(
                SEARCH_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {SEARCH_KEY}",
                    "content-type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                if response.status != 200:
                    raise GrocerError(f"grocer.nz search returned {response.status}")
                body = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise GrocerError("grocer.nz search failed") from err

        products: list[GrocerProduct] = []
        for hit in body.get("hits") or []:
            if not isinstance(hit, dict) or hit.get("id") is None:
                continue
            stores = hit.get("stores")
            products.append(
                GrocerProduct(
                    int(hit["id"]),
                    str(hit.get("name") or ""),
                    str(hit.get("brand") or ""),
                    "" if hit.get("size") is None else str(hit["size"]),
                    {int(s) for s in stores} if isinstance(stores, list) else None,
                )
            )
        return products

    # -- per-store prices -------------------------------------------------

    async def _async_download(self, url: str, target: Path) -> int:
        """Download to a temporary file, then replace, so a partial write never wins."""

        target.parent.mkdir(parents=True, exist_ok=True)
        # Unique per attempt: two profiles share one mirror directory, so a fixed
        # ".part" name would let them overwrite each other's download.
        temp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.part")
        try:
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=180)
            ) as response:
                if response.status != 200:
                    raise GrocerError(f"grocer.nz returned {response.status}")
                size = 0
                with temp.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(65536):
                        handle.write(chunk)
                        size += len(chunk)
        except (aiohttp.ClientError, TimeoutError) as err:
            temp.unlink(missing_ok=True)
            raise GrocerError("grocer.nz download failed") from err
        except OSError as err:
            # Disk full, permission denied, and similar: never leave a .part.
            temp.unlink(missing_ok=True)
            raise GrocerError("grocer.nz download could not be written") from err
        except GrocerError:
            temp.unlink(missing_ok=True)
            raise
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        try:
            temp.replace(target)
        except OSError as err:
            temp.unlink(missing_ok=True)
            raise GrocerError("grocer.nz mirror could not be replaced") from err
        self._mirror_bytes = None
        return size

    async def async_sync_store_prices(self, store_id: int) -> bool:
        async with self._lock(f"store-{store_id}"):
            target = self._dir / f"prices_{store_id}.parquet"
            try:
                await self._async_download(
                    STORE_PRICES_URL.format(store=store_id), target
                )
            except GrocerError as err:
                _LOGGER.debug("grocer.nz store sync failed: %s", err)
                return False
            self._store_prices.pop(store_id, None)
            return True

    def store_prices(self, store_id: int) -> dict[int, GrocerPrice]:
        """Return mirrored prices for a store, or empty when none are available."""

        if store_id in self._store_prices:
            return self._store_prices[store_id]
        path = self._dir / f"prices_{store_id}.parquet"
        if not path.exists():
            return {}
        try:
            columns = read_columns(
                str(path),
                ["product_id", "original_price_cent", "sale_price_cent", "updated_at"],
            )
        except ParquetError as err:
            raise GrocerError("grocer.nz store prices could not be read") from err
        rows = list(
            zip(
                columns["product_id"],
                columns["original_price_cent"],
                columns["sale_price_cent"],
                columns["updated_at"],
                strict=False,
            )
        )
        prices: dict[int, GrocerPrice] = {}
        for product_id, regular, sale, updated in rows:
            if regular is None:
                continue
            prices[int(product_id)] = GrocerPrice(
                int(product_id),
                round(float(regular) / 100, 2),
                round(float(sale) / 100, 2) if sale is not None else None,
                str(updated) if updated is not None else None,
            )
        self._store_prices[store_id] = prices
        return prices

    # -- history ----------------------------------------------------------

    async def async_sync_history(self, product_id: int) -> bool:
        """Mirror one product's history file, which covers every store."""

        async with self._lock(f"history-{product_id}"):
            target = self._dir / f"history_{product_id}.parquet"
            if target.exists():
                return True
            try:
                await self._async_download(
                    PRODUCT_HISTORY_URL.format(product=product_id), target
                )
            except GrocerError as err:
                _LOGGER.debug("grocer.nz history sync failed: %s", err)
                return False
            return True

    def history(self, product_id: int, store_id: int) -> list[tuple[float, float]]:
        """Return ``[(timestamp, price)]`` for one product at one store."""

        path = self._dir / f"history_{product_id}.parquet"
        if not path.exists():
            return []
        try:
            columns = read_columns(str(path), ["updated_at", "store_id", "price_cent"])
        except ParquetError as err:
            raise GrocerError("grocer.nz history could not be read") from err
        rows = [
            (when, cents)
            for when, row_store, cents in zip(
                columns["updated_at"],
                columns["store_id"],
                columns["price_cent"],
                strict=False,
            )
            if row_store == store_id
        ]
        series: list[tuple[float, float]] = []
        for when, cents in rows:
            if when is None or cents is None:
                continue
            try:
                # updated_at is epoch milliseconds in the parquet file.
                series.append((int(when) / 1000, round(float(cents) / 100, 2)))
            except (TypeError, ValueError):
                continue
        series.sort(key=lambda point: point[0])
        return series

    # -- matching ---------------------------------------------------------

    async def async_match(
        self,
        store_id: int,
        name: str,
        price: float | None,
        *,
        tolerance: float = PRICE_TOLERANCE,
    ) -> GrocerMatch | None:
        """Find the catalogue product behind a retailer's resolved product.

        A name match alone is not trusted: the candidate's price must agree with
        the price the retailer just quoted, either its regular or its sale price.
        That makes an otherwise ambiguous name match self-validating, and it is
        why this can safely import history rather than merely guess.
        """

        if price is None:
            return None
        prices = await self.hass.async_add_executor_job(self.store_prices, store_id)
        if not prices:
            return None
        target = _normalise(name)
        if not target:
            return None

        products = await self._async_search(name)
        scored: list[tuple[float, GrocerProduct]] = []
        for product in products:
            if product.product_id not in prices:
                continue
            if product.stores is not None and store_id not in product.stores:
                continue
            if not _sizes_compatible(name, product.label):
                continue
            ratio = SequenceMatcher(None, target, _normalise(product.label)).ratio()
            if ratio >= MIN_NAME_RATIO:
                scored.append((ratio, product))
        # Search already ranks by popularity, so ties keep that order.
        scored.sort(key=lambda item: item[0], reverse=True)

        for ratio, product in scored[:25]:
            row = prices[product.product_id]
            if abs(row.regular - price) <= tolerance or (
                row.sale is not None and abs(row.sale - price) <= tolerance
            ):
                history = await self.hass.async_add_executor_job(
                    self.history, product.product_id, store_id
                )
                return GrocerMatch(
                    product=product,
                    price=row,
                    history=history,
                    name_ratio=ratio,
                )
        return None

    async def async_match_with_history(
        self, store_id: int, name: str, price: float | None
    ) -> GrocerMatch | None:
        """Match, ensuring the history file is mirrored first."""

        match = await self.async_match(store_id, name, price)
        if match is None:
            return None
        if not match.history:
            if not await self.async_sync_history(match.product.product_id):
                return match
            match = await self.async_match(store_id, name, price)
        return match

    # -- housekeeping -----------------------------------------------------

    def _scan_mirrored_bytes(self) -> int:
        """Filesystem walk; only call from an executor thread."""

        if not self._dir.exists():
            return 0
        return sum(p.stat().st_size for p in self._dir.glob("*") if p.is_file())

    async def async_mirrored_bytes(self) -> int:
        """Mirror size for diagnostics, measured off the event loop."""

        if self._mirror_bytes is None:
            self._mirror_bytes = await self.hass.async_add_executor_job(
                self._scan_mirrored_bytes
            )
        return self._mirror_bytes

    def prune_history(self, keep_products: set[int], maximum: int) -> int:
        """Trim mirrored history files down to ``maximum``.

        History is kept deliberately, so this never prunes by list membership: it
        only bounds total size, dropping the least recently touched files first.
        Products still on a list are always spared.
        """

        if not self._dir.exists():
            return 0
        files = sorted(
            (
                (path.stat().st_mtime, path)
                for path in self._dir.glob("history_*.parquet")
                if path.is_file()
            ),
            key=lambda item: item[0],
        )
        removed = 0
        for _mtime, path in files:
            if len(files) - removed <= maximum:
                break
            try:
                product_id = int(path.stem.split("_", 1)[1])
            except (IndexError, ValueError):
                product_id = -1
            if product_id in keep_products:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        if removed:
            self._mirror_bytes = None
        return removed
