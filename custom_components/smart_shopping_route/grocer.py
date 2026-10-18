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
import threading
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import GROCER_MIRROR_DIR

_LOGGER = logging.getLogger(__name__)

ASSET_BASE = "https://assets-prod.grocer.nz/public"
CATALOGUE_URL = f"{ASSET_BASE}/base_v3.duckdb.br"
STORE_PRICES_URL = f"{ASSET_BASE}/prices_per_store_v3/public_prices_{{store}}.parquet"
PRODUCT_HISTORY_URL = f"{ASSET_BASE}/price_history_v3/price_history_{{product}}.parquet"

CATALOGUE_FILE = "base_v3.duckdb"
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
        self._duckdb: Any = None
        self._catalogue: dict[int, GrocerProduct] | None = None
        self._stores: dict[int, str] | None = None
        self._store_prices: dict[int, dict[int, GrocerPrice]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Every query runs in an executor thread, and a duckdb connection is not
        # safe for concurrent use from several threads: doing so returns
        # malformed result sets rather than raising. All access is serialised.
        self._db_lock = threading.Lock()

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
        """grocer.nz data needs duckdb; the integration works without it."""

        try:
            import duckdb  # noqa: F401
        except ImportError:
            return False
        return True

    def _connect(self) -> Any:
        if self._duckdb is None:
            try:
                import duckdb
            except ImportError as err:  # pragma: no cover - import guard
                raise GrocerError("duckdb is not installed") from err
            path = self._dir / CATALOGUE_FILE
            if not path.exists():
                raise GrocerError("grocer.nz catalogue is not mirrored yet")
            self._duckdb = duckdb.connect(str(path), read_only=True)
        return self._duckdb

    def _query(self, sql: str, params: list | None = None) -> list[tuple]:
        """Run one query, serialised against every other query.

        A duckdb connection cannot be shared between threads: concurrent use
        returns malformed result sets instead of raising, which silently
        corrupts matches. Every call site goes through here.
        """

        con = self._connect()
        try:
            with self._db_lock:
                return con.execute(sql, params or []).fetchall()
        except Exception as err:
            raise GrocerError("grocer.nz data could not be read") from err

    async def _async_download(self, url: str, target: Path) -> int:
        """Download to a temporary file, then replace, so a partial write never wins."""

        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".part")
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
        except GrocerError:
            temp.unlink(missing_ok=True)
            raise
        temp.replace(target)
        return size

    # -- catalogue --------------------------------------------------------

    async def async_sync_catalogue(self) -> bool:
        """Mirror the catalogue. Returns whether a fresh copy was fetched."""

        async with self._lock("catalogue"):
            try:
                await self._async_download(CATALOGUE_URL, self._dir / CATALOGUE_FILE)
            except GrocerError as err:
                # Keep serving the mirrored copy; history is the point of this.
                _LOGGER.debug("grocer.nz catalogue sync failed: %s", err)
                return False
            self._catalogue = None
            self._stores = None
            return True

    def _ensure_catalogue(self) -> None:
        if self._catalogue is not None and self._stores is not None:
            return
        products = self._query("select id, name, brand, size from public_products")
        stores = self._query("select id, name from public_stores")
        self._catalogue = {
            int(pid): GrocerProduct(
                int(pid),
                str(name or ""),
                str(brand or ""),
                "" if size is None else str(size),
            )
            for pid, name, brand, size in products
        }
        self._stores = {int(sid): str(name) for sid, name in stores}

    def stores(self) -> dict[int, str]:
        self._ensure_catalogue()
        assert self._stores is not None
        return dict(self._stores)

    def resolve_store(self, wanted: str) -> int | None:
        """Find a store id by name, tolerating punctuation and banner style.

        The catalogue writes "PAK'nSAVE Ormiston" while people write
        "PakNSave Ormiston", so apostrophes and case must not matter.
        """

        key = _normalise(wanted)
        if not key:
            return None
        stores = self.stores()
        for store_id, name in stores.items():
            if _normalise(name) == key:
                return store_id
        partial = [
            store_id for store_id, name in stores.items() if key in _normalise(name)
        ]
        if len(partial) == 1:
            return partial[0]
        return None

    # -- per-store prices -------------------------------------------------

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
        rows = self._query(
            "select product_id, original_price_cent, sale_price_cent, updated_at"
            " from read_parquet(?)",
            [str(path)],
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
        rows = self._query(
            "select updated_at, price_cent from read_parquet(?)"
            " where store_id = ? order by updated_at",
            [str(path), store_id],
        )
        series: list[tuple[float, float]] = []
        for when, cents in rows:
            if when is None or cents is None:
                continue
            try:
                series.append((when.timestamp(), round(float(cents) / 100, 2)))
            except (AttributeError, ValueError, OSError):
                continue
        return series

    # -- matching ---------------------------------------------------------

    def match(
        self,
        store_id: int,
        name: str,
        price: float | None,
        *,
        tolerance: float = PRICE_TOLERANCE,
    ) -> GrocerMatch | None:
        """Find the catalogue product behind a retailer's resolved product.

        A name match alone is not trusted: the candidate's price must agree with
        the price the retailer just quoted, either its regular or its sale
        price. That makes an otherwise ambiguous name match self-validating, and
        it is why this can safely import history rather than merely guess.
        """

        if price is None:
            return None
        prices = self.store_prices(store_id)
        if not prices:
            return None
        self._ensure_catalogue()
        assert self._catalogue is not None
        target = _normalise(name)
        if not target:
            return None

        candidates: list[tuple[float, GrocerProduct]] = []
        for product_id in prices:
            product = self._catalogue.get(product_id)
            if product is None:
                continue
            if not _sizes_compatible(name, product.label):
                continue
            ratio = SequenceMatcher(None, target, _normalise(product.label)).ratio()
            if ratio >= MIN_NAME_RATIO:
                candidates.append((ratio, product))
        candidates.sort(key=lambda item: item[0], reverse=True)

        for ratio, product in candidates[:25]:
            row = prices[product.product_id]
            if abs(row.regular - price) <= tolerance or (
                row.sale is not None and abs(row.sale - price) <= tolerance
            ):
                return GrocerMatch(
                    product=product,
                    price=row,
                    history=self.history(product.product_id, store_id),
                    name_ratio=ratio,
                )
        return None

    async def async_match_with_history(
        self, store_id: int, name: str, price: float | None
    ) -> GrocerMatch | None:
        """Match, ensuring the history file is mirrored first."""

        match = await self.hass.async_add_executor_job(
            self.match, store_id, name, price
        )
        if match is None:
            return None
        if not match.history:
            await self.async_sync_history(match.product.product_id)
            match = await self.hass.async_add_executor_job(
                self.match, store_id, name, price
            )
        return match

    # -- housekeeping -----------------------------------------------------

    def mirrored_bytes(self) -> int:
        if not self._dir.exists():
            return 0
        return sum(p.stat().st_size for p in self._dir.glob("*") if p.is_file())

    def prune_history(self, keep_products: set[int]) -> int:
        """Delete mirrored history for products no longer on any list."""

        if not self._dir.exists():
            return 0
        removed = 0
        for path in self._dir.glob("history_*.parquet"):
            try:
                product_id = int(path.stem.split("_", 1)[1])
            except (IndexError, ValueError):
                continue
            if product_id not in keep_products:
                path.unlink(missing_ok=True)
                removed += 1
        return removed
