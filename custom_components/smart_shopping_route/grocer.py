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
* Automatic matching is conservative: names, explicit pack sizes and prices
  must agree, and ambiguous sibling products are refused. Catalogue slugs often
  omit pack sizes, so an explicit history pin is needed for those products.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import GROCER_MIRROR_DIR, GROCER_STORE_REFRESH_HOURS
from .parquet import ParquetError, read_columns
from .prices import size_from_text

_LOGGER = logging.getLogger(__name__)

ASSET_BASE = "https://assets-prod.grocer.nz/public"
STORE_PRICES_URL = f"{ASSET_BASE}/prices_per_store_v3/public_prices_{{store}}.parquet"
PRODUCT_HISTORY_URL = f"{ASSET_BASE}/price_history_v3/price_history_{{product}}.parquet"

# Product names come from grocer.nz's sitemap: 114k URLs of the form
# /product/<id>/<slug>, which is public, needs no credentials, and covers every
# product in the catalogue. Slugs are the only name source that needs neither a
# compiled reader nor an API key.
SITEMAP_URLS = tuple(f"{ASSET_BASE}/sitemap_{index}.txt" for index in range(3))
NAMES_FILE = "names.txt"
_STORE_COLUMNS = ("product_id", "original_price_cent", "sale_price_cent", "updated_at")
_HISTORY_COLUMNS = ("updated_at", "store_id", "price_cent")
# A price match must be within this many cents to count as the same product.
PRICE_TOLERANCE = 0.02
# Minimum name similarity before a candidate is even considered.
MIN_NAME_RATIO = 0.85


# Pack sizes must agree, because two sizes of the same product often share a
# price and price agreement alone would then accept the wrong one.
def _sizes(text: str) -> set[str]:
    """Use the same whole-pack dimensions as current-price matching."""
    parsed = size_from_text(str(text))
    return {f"{parsed[1]:g}{parsed[2]}"} if parsed else set()


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


_PRODUCT_URL_RE = re.compile(r"/product/(\d+)/([^/?#]+)")


def _parse_product_url(url: str) -> tuple[int, str] | None:
    """Turn ``/product/6403/sanitarium-weet-bix`` into ``(6403, "sanitarium weet-bix")``."""

    match = _PRODUCT_URL_RE.search(url)
    if not match:
        return None
    slug = match.group(2).replace("-", " ").strip()
    if not slug:
        return None
    return int(match.group(1)), slug


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
        self._names: dict[int, str] | None = None
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

    async def async_sync_names(self) -> bool:
        """Mirror the product name index built from grocer.nz's sitemaps."""

        async with self._lock("names"):
            texts = []
            for url in SITEMAP_URLS:
                text = await self._async_download_text(url)
                if text is None:
                    return False
                texts.append(text)
            if not await self.hass.async_add_executor_job(
                self._replace_names, self._dir / NAMES_FILE, texts
            ):
                return False
            self._names = None
            self._mirror_bytes = None
            return True

    @staticmethod
    def _replace_names(target: Path, texts: list[str]) -> bool:
        # Parsing the public catalogue and file writes belong off HA's loop.
        lines = [
            f"{parsed[0]}\t{parsed[1]}"
            for text in texts
            for line in text.splitlines()
            if (parsed := _parse_product_url(line)) is not None
        ]
        if not lines:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.part")
        try:
            temp.write_text("\n".join(lines), encoding="utf-8")
            temp.replace(target)
        except OSError as err:
            temp.unlink(missing_ok=True)
            raise GrocerError("grocer.nz names could not be written") from err
        return True

    async def _async_download_text(self, url: str) -> str | None:
        try:
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status != 200:
                    raise GrocerError(f"grocer.nz returned {response.status}")
                return await response.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GrocerError("grocer.nz download failed") from err

    def names(self) -> dict[int, str]:
        """Return ``{product_id: name}`` from the mirrored sitemap index."""

        if self._names is not None:
            return self._names
        path = self._dir / NAMES_FILE
        if not path.exists():
            return {}
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as err:
            raise GrocerError("grocer.nz names could not be read") from err
        names: dict[int, str] = {}
        for line in text.splitlines():
            product_id, _sep, name = line.partition("\t")
            if name:
                try:
                    names[int(product_id)] = name
                except ValueError:
                    continue
        self._names = names
        return names

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
                        if size > 64 * 1024 * 1024:
                            raise GrocerError(
                                "grocer.nz asset exceeds the 64 MiB safety limit"
                            )
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
            async with self._lock("parquet-decode"):
                await self.hass.async_add_executor_job(
                    self._validate_mirror, temp, target.name
                )
            temp.replace(target)
        except (ParquetError, ValueError) as err:
            temp.unlink(missing_ok=True)
            raise GrocerError(
                "grocer.nz asset is invalid; retaining the previous mirror"
            ) from err
        except asyncio.CancelledError:
            temp.unlink(missing_ok=True)
            raise
        except OSError as err:
            temp.unlink(missing_ok=True)
            raise GrocerError("grocer.nz mirror could not be replaced") from err
        self._mirror_bytes = None
        return size

    @staticmethod
    def _validate_mirror(path: Path, name: str) -> None:
        fields = list(
            _STORE_COLUMNS if name.startswith("prices_") else _HISTORY_COLUMNS
        )
        read_columns(str(path), fields)

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
                list(_STORE_COLUMNS),
            )
        except ParquetError as err:
            raise GrocerError("grocer.nz store prices could not be read") from err
        rows = list(
            zip(
                columns["product_id"],
                columns["original_price_cent"],
                columns["sale_price_cent"],
                columns["updated_at"],
                strict=True,
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
            fresh = await self.hass.async_add_executor_job(self._history_fresh, target)
            if fresh:
                return True
            try:
                await self._async_download(
                    PRODUCT_HISTORY_URL.format(product=product_id), target
                )
            except GrocerError as err:
                _LOGGER.debug("grocer.nz history sync failed: %s", err)
                return target.exists()
            return True

    @staticmethod
    def _history_fresh(target: Path) -> bool:
        try:
            return (
                0
                <= time.time() - target.stat().st_mtime
                < GROCER_STORE_REFRESH_HOURS * 3600
            )
        except OSError:
            return False

    async def _async_history(
        self, product_id: int, store_id: int
    ) -> list[tuple[float, float]]:
        # A product asset includes all stores; serialize large decodes rather
        # than decoding a grocery list concurrently in HA's thread pool.
        async with self._lock("parquet-decode"):
            return await self.hass.async_add_executor_job(
                self.history, product_id, store_id
            )

    def history(self, product_id: int, store_id: int) -> list[tuple[float, float]]:
        """Return ``[(timestamp, price)]`` for one product at one store."""

        path = self._dir / f"history_{product_id}.parquet"
        if not path.exists():
            return []
        try:
            columns = read_columns(str(path), list(_HISTORY_COLUMNS))
        except ParquetError as err:
            raise GrocerError("grocer.nz history could not be read") from err
        rows = [
            (when, cents)
            for when, row_store, cents in zip(
                columns["updated_at"],
                columns["store_id"],
                columns["price_cent"],
                strict=True,
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
        """Resolve only a strong, unambiguous name/pack/price match.

        Price agreement is corroboration, not product identity. If the retailer
        states a pack size but the slug omits it, require an explicit pin rather
        than attaching a potentially unrelated history.
        """

        if price is None:
            return None
        prices = await self.hass.async_add_executor_job(self.store_prices, store_id)
        if not prices:
            return None
        names = await self.hass.async_add_executor_job(self.names)
        if not names:
            return None
        target = _normalise(name)
        if not target:
            return None

        # Cheap prefilter: a candidate with no word in common cannot score well,
        # and skipping it keeps this linear-ish instead of comparing every
        # product at the store on every item.
        target_tokens = set(_normalise(name).split())
        scored: list[tuple[float, int, str]] = []
        for product_id in prices:
            label = names.get(product_id)
            if not label:
                continue
            if not target_tokens & set(label.split()):
                continue
            if not _sizes_compatible(name, label) or (
                _sizes(name) and not _sizes(label)
            ):
                continue
            ratio = SequenceMatcher(None, target, _normalise(label)).ratio()
            # Don't silently substitute a missing brand/variant token.
            if ratio >= MIN_NAME_RATIO and target_tokens <= set(
                _normalise(label).split()
            ):
                scored.append((ratio, product_id, label))
        scored.sort(key=lambda item: item[0], reverse=True)

        agreeing = [
            candidate
            for candidate in scored
            if abs(prices[candidate[1]].regular - price) <= tolerance
            or (
                prices[candidate[1]].sale is not None
                and abs(prices[candidate[1]].sale - price) <= tolerance
            )
        ]
        if not agreeing or (
            len(agreeing) > 1 and agreeing[0][0] - agreeing[1][0] < 0.1
        ):
            return None
        ratio, product_id, label = agreeing[0]
        history = await self._async_history(product_id, store_id)
        return GrocerMatch(
            product=GrocerProduct(product_id, label),
            price=prices[product_id],
            history=history,
            name_ratio=ratio,
        )

    async def async_pinned_match(
        self, store_id: int, product_id: int
    ) -> GrocerMatch | None:
        """Return the history for one explicitly chosen product.

        This is the escape hatch for variants that slugs cannot distinguish: a
        user who pins the product gets exactly that product's history, with no
        guessing. Nothing is returned when the store does not stock it.
        """

        prices = await self.hass.async_add_executor_job(self.store_prices, store_id)
        row = prices.get(product_id)
        if row is None:
            return None
        names = await self.hass.async_add_executor_job(self.names)
        label = names.get(product_id, str(product_id))
        if not await self.async_sync_history(product_id):
            return None
        history = await self._async_history(product_id, store_id)
        if not history:
            return None
        return GrocerMatch(
            product=GrocerProduct(product_id, label),
            price=row,
            history=history,
            name_ratio=1.0,
        )

    async def async_match_with_history(
        self, store_id: int, name: str, price: float | None
    ) -> GrocerMatch | None:
        """Match, ensuring the history file is mirrored first."""

        match = await self.async_match(store_id, name, price)
        if match is None:
            return None
        if not await self.async_sync_history(match.product.product_id):
            return match
        match.history = await self._async_history(match.product.product_id, store_id)
        return match

    # -- housekeeping -----------------------------------------------------

    def _scan_mirrored_bytes(self) -> int:
        """Filesystem walk; only call from an executor thread."""

        if not self._dir.exists():
            return 0
        total = 0
        for path in self._dir.glob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

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
        files = []
        for path in self._dir.glob("history_*.parquet"):
            try:
                files.append((path.stat().st_mtime, path))
            except FileNotFoundError:
                continue
            except OSError as err:
                raise GrocerError(
                    "grocer.nz mirror metadata could not be read"
                ) from err
        files.sort(key=lambda item: item[0])
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
            try:
                path.unlink(missing_ok=True)
            except OSError as err:
                raise GrocerError("grocer.nz mirror could not be pruned") from err
            removed += 1
        if removed:
            self._mirror_bytes = None
        return removed
