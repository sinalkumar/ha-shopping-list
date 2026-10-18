"""Anonymous price lookups for New Zealand supermarkets.

Both chains expose read-only public endpoints that work without an account, so
this module needs no credentials and no browser. It sends a shopping item name
to the retailer's product search, which is the only third party involved.

Privacy: item names are never logged here. Logs carry counts and chain names
only.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

_LOGGER = logging.getLogger(__name__)

CHAIN_WOOLWORTHS = "woolworths"
CHAIN_PAKNSAVE = "paknsave"
CHAIN_NONE = "none"
CHAINS = (CHAIN_NONE, CHAIN_WOOLWORTHS, CHAIN_PAKNSAVE)

# A candidate must share at least this share of the item's tokens. Below it we
# show no price rather than a confidently wrong one.
MIN_MATCH_SCORE = 0.6
# Two tokens are the same word if they are this similar, so "strawberries"
# matches "strawberry" and "Weet-Bix" matches "Weetbix".
TOKEN_SIMILARITY = 0.8
TOKEN_MIN_LENGTH = 4
SEARCH_PAGE_SIZE = 8

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
# Retailers word things differently from shopping lists. Only add pairs that are
# unambiguous for groceries.
_SYNONYMS = {
    "paper": {"tissue"},
    "tissue": {"paper"},
}
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_PAREN_RE = re.compile(r"\([^)]*\)")

WOOLWORTHS_URL = "https://www.woolworths.co.nz/api/graphql"
PAKNSAVE_TOKEN_URL = "https://www.paknsave.co.nz/api/user/get-current-user"
PAKNSAVE_API = "https://api-prod.paknsave.co.nz"
PAKNSAVE_STORES_URL = f"{PAKNSAVE_API}/v1/edge/store"
PAKNSAVE_SEARCH_URL = f"{PAKNSAVE_API}/v1/edge/search/paginated/products"

_WOOLWORTHS_QUERY = """query ProductSearch($searchInput: CompositeSearchInput!) {
  My { products(searchInput: $searchInput) { results { __typename
    ... on ProductSummary { sku productName
      variants { variantPrice { sellingPrice wasPrice isSpecial } } } } } } }"""


class PriceError(Exception):
    """Raised when a price lookup cannot be completed."""


@dataclass(frozen=True, slots=True)
class ProductPin:
    """A user's decision about which product an item means.

    ``query`` replaces the item's own text as the search term, which is how a
    household shorthand ("Kids vitamins") reaches a real product name.
    ``product_id`` pins one exact product, so an ambiguous family such as the
    30-pack and 110-pack of the same gummies can never be confused.
    """

    chain: str
    query: str = ""
    product_id: str | None = None
    product_name: str | None = None
    # grocer.nz product id, used only for price history. Set it when automatic
    # matching picks a sibling variant, which slugs cannot distinguish.
    grocer_product_id: int | None = None

    @property
    def match_target(self) -> str:
        """Text to score candidates against: the pin wins over the item text."""

        return self.query.strip() or ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain,
            "query": self.query,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "grocer_product_id": self.grocer_product_id,
        }

    @classmethod
    def from_dict(cls, chain: str, data: dict[str, Any]) -> ProductPin:
        return cls(
            chain=chain,
            query=str(data.get("query") or ""),
            product_id=(str(data["product_id"]) if data.get("product_id") else None),
            product_name=(
                str(data["product_name"]) if data.get("product_name") else None
            ),
            grocer_product_id=(
                int(data["grocer_product_id"])
                if data.get("grocer_product_id")
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """A matched price for one shopping item."""

    chain: str
    product_id: str
    product_name: str
    price: float
    was_price: float | None = None
    valid_until: str | None = None
    score: float = 0.0
    # Retailer says a promotion record is active. This does not imply a lower
    # price: most Foodstuffs promotions are price locks whose reward value
    # equals the shelf price, so it is reported as context, not as savings.
    on_promotion: bool = False
    # Pack size as the retailer states it ("1.2kg", "500g"), and the unit price
    # they quote or that we compute from the size ("$6.66/kg").
    size: str | None = None
    unit_price: str | None = None

    @property
    def is_special(self) -> bool:
        return self.was_price is not None and self.was_price > self.price

    @property
    def savings(self) -> float:
        if not self.is_special or self.was_price is None:
            return 0.0
        return round(self.was_price - self.price, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "price": self.price,
            "was_price": self.was_price,
            "valid_until": self.valid_until,
            "is_special": self.is_special,
            "on_promotion": self.on_promotion,
            "size": self.size,
            "unit_price": self.unit_price,
        }


# Sizes as retailers write them, including Woolworths' "1.2kg" style and packs.
_SIZE_IN_TEXT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(kg|g|ml|l|pk|pack|sheets)\b", re.IGNORECASE
)
_UNIT_LABELS = {
    "kg": ("kg", 1.0),
    "g": ("kg", 0.001),
    "l": ("L", 1.0),
    "ml": ("L", 0.001),
}


def size_from_text(text: str) -> tuple[str, float, str] | None:
    """Extract a pack size from a product name.

    Returns ``(label, amount_in_base_unit, base_unit)`` so a unit price can be
    computed, or None when the name states no size.
    """

    match = _SIZE_IN_TEXT.search(text or "")
    if not match:
        return None
    try:
        amount = float(match.group(1))
    except ValueError:
        return None
    written = match.group(2)
    unit = written.lower()
    if unit in _UNIT_LABELS:
        base_unit, factor = _UNIT_LABELS[unit]
        return (f"{match.group(1)}{written}", amount * factor, base_unit)
    # Countable packs are compared per pack, which is what the label already says.
    return (f"{match.group(1)}{written}", 0.0, "")


def compute_unit_price(price: float, size: tuple[str, float, str] | None) -> str | None:
    """Return a per-unit price such as ``$6.66/kg``, or None when unknowable."""

    if size is None or price <= 0:
        return None
    _label, amount, base_unit = size
    if not base_unit or amount <= 0:
        return None
    return f"${price / amount:.2f}/{base_unit}"


def _comparative_unit_price(comparative: dict[str, Any]) -> str | None:
    """Format a retailer's own comparative price, e.g. ``$1.48/100g``."""

    per_unit = comparative.get("pricePerUnit")
    quantity = comparative.get("unitQuantity")
    uom = comparative.get("unitQuantityUom")
    if per_unit is None or quantity is None or not uom:
        return None
    try:
        amount = float(quantity)
        # "per 1L" reads badly; "per L" is how the shelf ticket shows it.
        basis = f"{amount:g}{uom}" if amount != 1 else str(uom)
        return f"${float(per_unit) / 100:.2f}/{basis}"
    except (TypeError, ValueError):
        return None


def _size_from_comparative(price: float, comparative: dict[str, Any]) -> str | None:
    """Derive a pack size from the retailer's own per-unit figures.

    Foodstuffs product names usually omit the size, but their comparative price
    gives a price per 100g (or per unit), so the pack size follows from the shelf
    price. This is arithmetic on the retailer's own numbers, not a guess.

    Countable units are only reported when they look like a pack a shopper would
    recognise; a toilet roll's sheet count is not a useful "size".
    """

    per_unit = comparative.get("pricePerUnit")
    quantity = comparative.get("unitQuantity")
    uom = comparative.get("unitQuantityUom")
    if not per_unit or not quantity or not uom or price <= 0:
        return None
    try:
        total = float(price) * float(quantity) / (float(per_unit) / 100)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if total <= 0:
        return None

    # Normalise to the smaller unit so 0.5L is reported as 500ml.
    if uom == "L":
        total, uom = total * 1000, "ml"
    elif uom == "kg":
        total, uom = total * 1000, "g"

    if uom in ("g", "ml"):
        # Round to the nearest 5, so 499.3 becomes 500 rather than looking exact.
        rounded = round(total / 5) * 5
        if rounded >= 1000:
            base = "kg" if uom == "g" else "L"
            return f"{rounded / 1000:.1f}".rstrip("0").rstrip(".") + base
        return f"{rounded:.0f}{uom}" if rounded >= 5 else None

    if uom in ("ea", "pk", "pack") and 1 <= total <= 100:
        return f"{total:.0f}{uom}"

    # Sheet counts and similar say nothing useful about the pack.
    return None


def _tokens(text: str) -> list[str]:
    """Split text into comparable words.

    Hyphens and punctuation are removed rather than split so that "Weet-Bix"
    stays one token and matches "Weetbix".
    """

    return _WORD_RE.findall(text.lower())


def _same_word(left: str, right: str) -> bool:
    if left == right:
        return True
    if min(len(left), len(right)) < TOKEN_MIN_LENGTH:
        return False
    if left.startswith(right) or right.startswith(left):
        return True
    return SequenceMatcher(None, left, right).ratio() >= TOKEN_SIMILARITY


def match_score(summary: str, candidate: str) -> float:
    """Return the share of the item's words present in a candidate name."""

    item_tokens = _tokens(summary)
    if not item_tokens:
        return 0.0
    candidate_tokens = _tokens(candidate)
    matched = 0
    for token in item_tokens:
        if any(_same_word(token, other) for other in candidate_tokens):
            matched += 1
        elif any(
            _same_word(other, synonym)
            for synonym in _SYNONYMS.get(token, ())
            for other in candidate_tokens
        ):
            matched += 1
    return matched / len(item_tokens)


def search_term(summary: str, *, keep_qualifier: bool = False) -> str:
    """Return the search text for an item.

    Parentheticals are usually qualifiers rather than the product noun, and
    searching them whole returns unrelated products: "Tuna (olive oil)" matches
    bottles of olive oil. The query therefore drops them, while matching still
    scores against the full summary. ``keep_qualifier`` re-adds the qualifier
    words for the retry when the plain term finds nothing good.
    """

    base = _PAREN_RE.sub(" ", summary).strip() or summary.strip()
    if not keep_qualifier:
        return base
    qualifiers = " ".join(_PAREN_RE.findall(summary)).strip("() ")
    if not qualifiers:
        return base
    return f"{base} {qualifiers}".strip()


def best_match(summary: str, candidates: list[PriceQuote]) -> PriceQuote | None:
    """Pick the closest candidate, or None when nothing is close enough.

    The score acts as a filter, and the retailer's own relevance order breaks
    ties. That ordering carries signal the tokens do not: for "Strawberries" it
    puts fresh punnets above frozen ones, chocolate bites, and wafers, all of
    which contain the word.
    """

    best: PriceQuote | None = None
    best_score = 0.0
    for candidate in candidates:
        score = match_score(summary, candidate.product_name)
        if score < MIN_MATCH_SCORE or score <= best_score:
            continue
        best_score = score
        best = PriceQuote(
            chain=candidate.chain,
            product_id=candidate.product_id,
            product_name=candidate.product_name,
            price=candidate.price,
            was_price=candidate.was_price,
            valid_until=candidate.valid_until,
            score=score,
            on_promotion=candidate.on_promotion,
            size=candidate.size,
            unit_price=candidate.unit_price,
        )
    return best


class PriceClient:
    """Base class for a retailer price client."""

    chain: str = ""

    def __init__(self, hass: HomeAssistant, store_id: str | None = None) -> None:
        self.hass = hass
        self.store_id = store_id
        self._session: aiohttp.ClientSession | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        """Resolve a cookie-free session lazily so construction stays hass-free.

        A dedicated session with a dummy cookie jar keeps the documented claim
        that price lookups store nothing: the shared Home Assistant session
        would retain whatever cookies the retailers set.
        """

        if self._session is None:
            self._session = async_create_clientsession(
                self.hass, cookie_jar=aiohttp.DummyCookieJar()
            )
        return self._session

    async def async_candidates(self, term: str) -> list[PriceQuote]:
        raise NotImplementedError

    async def async_lookup(
        self, summary: str, pin: ProductPin | None = None
    ) -> PriceQuote | None:
        """Return the best price for one shopping item, or None.

        A user pin takes precedence over automatic matching: its query is used
        as the search term and its product ID, when set, must match exactly. An
        exact pin that cannot be found returns nothing rather than falling back
        to a guess, because a pin is a decision, not a hint.

        Without a pin, a qualifier in brackets is tried only after the plain
        term fails, so the common case stays a single request.
        """

        if pin is not None and pin.product_id:
            return self._exact(
                await self.async_candidates(self._pin_term(pin, summary)), pin
            )

        term = self._pin_term(pin, summary)
        target = pin.match_target if pin and pin.match_target else summary
        match = best_match(target, await self.async_candidates(term))
        if match is not None or (pin is not None and pin.match_target):
            return match
        qualified = search_term(summary, keep_qualifier=True)
        if qualified == search_term(summary):
            return None
        return best_match(summary, await self.async_candidates(qualified))

    @staticmethod
    def _pin_term(pin: ProductPin | None, summary: str) -> str:
        if pin is not None and pin.match_target:
            return pin.match_target
        return search_term(summary)

    @staticmethod
    def _exact(candidates: list[PriceQuote], pin: ProductPin) -> PriceQuote | None:
        for candidate in candidates:
            if candidate.product_id == pin.product_id:
                return candidate
        return None


class WoolworthsPriceClient(PriceClient):
    """Woolworths NZ product search.

    Anonymous search returns the online price, which is not store-scoped, so
    this chain needs no store selection.
    """

    chain = CHAIN_WOOLWORTHS

    async def async_candidates(self, term: str) -> list[PriceQuote]:
        payload = {
            "operationName": "ProductSearch",
            "variables": {
                "searchInput": {
                    "byKeyword": {
                        "value": term,
                        "sortBy": "RELEVANCE",
                        "pageIndex": 0,
                        "pageSize": SEARCH_PAGE_SIZE,
                    }
                }
            },
            "query": _WOOLWORTHS_QUERY,
        }
        try:
            async with self.session.post(
                WOOLWORTHS_URL,
                json=payload,
                headers={**_BROWSER_HEADERS, "content-type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=25),
            ) as response:
                if response.status != 200:
                    raise PriceError(f"Woolworths returned {response.status}")
                body = await response.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            raise PriceError("Woolworths search failed") from err

        if body.get("errors"):
            raise PriceError("Woolworths search returned errors")

        results = (
            ((body.get("data") or {}).get("My") or {}).get("products") or {}
        ).get("results") or []
        quotes: list[PriceQuote] = []
        for row in results:
            if not row.get("sku") or not row.get("productName"):
                continue
            variants = row.get("variants") or []
            price_block = (variants[0] or {}).get("variantPrice") if variants else None
            if not price_block:
                continue
            selling = price_block.get("sellingPrice")
            if selling is None:
                continue
            name = str(row["productName"])
            was = price_block.get("wasPrice")
            quotes.append(
                PriceQuote(
                    chain=self.chain,
                    product_id=str(row["sku"]),
                    product_name=name,
                    price=float(selling),
                    was_price=float(was) if was else None,
                    size=(parsed[0] if (parsed := size_from_text(name)) else None),
                    unit_price=compute_unit_price(float(selling), size_from_text(name)),
                )
            )
        return quotes


class PakNSavePriceClient(PriceClient):
    """Pak'nSave (Foodstuffs) product search, scoped to one store.

    Prices are genuinely per-store here, so a store ID is required. The API
    exposes no "was" price at all; the only honest specials signal is our own
    recorded price history, which the coordinator keeps.
    """

    chain = CHAIN_PAKNSAVE

    def __init__(self, hass: HomeAssistant, store_id: str | None = None) -> None:
        if not store_id:
            raise PriceError("A Pak'nSave store is required")
        super().__init__(hass, store_id)
        self._token: str | None = None
        self._token_expiry = 0.0
        self._token_lock = asyncio.Lock()

    async def _async_token(self) -> str:
        """Fetch or reuse an anonymous access token."""

        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expiry:
                return self._token
            try:
                async with self.session.post(
                    PAKNSAVE_TOKEN_URL,
                    json={},
                    headers={
                        **_BROWSER_HEADERS,
                        "Origin": "https://www.paknsave.co.nz",
                        "content-type": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=25),
                ) as response:
                    if response.status != 200:
                        raise PriceError(f"Pak'nSave token returned {response.status}")
                    body = await response.json(content_type=None)
            except (TimeoutError, aiohttp.ClientError, ValueError) as err:
                raise PriceError("Pak'nSave token fetch failed") from err
            token = body.get("access_token")
            if not token:
                raise PriceError("Pak'nSave token missing")
            self._token = str(token)
            # Tokens last far longer than a refresh interval; re-use generously
            # and refresh on any 401 anyway.
            self._token_expiry = time.monotonic() + 3600
            return self._token

    async def async_candidates(self, term: str) -> list[PriceQuote]:
        token = await self._async_token()
        payload = {
            "algoliaQuery": {"query": term},
            "storeId": self.store_id,
            "hitsPerPage": SEARCH_PAGE_SIZE,
            "page": 0,
            "sortOrder": "NI_POPULARITY_ASC",
        }
        try:
            async with self.session.post(
                PAKNSAVE_SEARCH_URL,
                json=payload,
                headers={
                    **_BROWSER_HEADERS,
                    "Authorization": f"Bearer {token}",
                    "content-type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=25),
            ) as response:
                if response.status == 401:
                    self._token = None
                    raise PriceError("Pak'nSave token rejected")
                if response.status != 200:
                    raise PriceError(f"Pak'nSave returned {response.status}")
                body = await response.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            raise PriceError("Pak'nSave search failed") from err

        quotes: list[PriceQuote] = []
        for product in body.get("products") or []:
            name = " ".join(
                part for part in (product.get("brand"), product.get("name")) if part
            ).strip()
            single = (product.get("singlePrice") or {}).get("price")
            if not name or single is None:
                continue
            # "promotions" is a dict on most products but a list on some, so
            # only read it when it actually carries the validity text.
            promotions = product.get("promotions")
            validity = (
                promotions.get("priceValidityFreeFormText")
                if isinstance(promotions, dict)
                else None
            )
            # The search payload carries the same promotion records as the
            # product detail endpoint, so no extra request is needed.
            on_promotion = bool(promotions)
            single_block = product.get("singlePrice") or {}
            comparative = single_block.get("comparativePrice") or {}
            shelf_price = round(float(single) / 100, 2)
            parsed_size = size_from_text(name)
            size_label = (
                parsed_size[0]
                if parsed_size
                else _size_from_comparative(shelf_price, comparative)
            )
            unit_price = _comparative_unit_price(comparative) or compute_unit_price(
                shelf_price, parsed_size
            )
            quotes.append(
                PriceQuote(
                    chain=self.chain,
                    product_id=str(product.get("productId") or name),
                    product_name=name,
                    price=shelf_price,
                    was_price=None,
                    valid_until=str(validity) if validity else None,
                    on_promotion=on_promotion,
                    size=size_label,
                    unit_price=unit_price,
                )
            )
        return quotes


async def async_list_paknsave_stores(
    hass: HomeAssistant, session: aiohttp.ClientSession | None = None
) -> dict[str, str]:
    """Return ``{store_id: store_name}`` for Pak'nSave.

    Used by the config flow so a profile can be pointed at a real store without
    hand-copying a UUID. Any session created here is closed again, because a
    config flow runs many times and a leaked session lives until restart.
    """

    owns_session = session is None
    client = PakNSavePriceClient(hass, store_id="browse")
    if session is not None:
        client._session = session
    try:
        return await _async_fetch_stores(client)
    finally:
        if owns_session and client._session is not None:
            await client._session.close()
            client._session = None


async def _async_fetch_stores(client: PakNSavePriceClient) -> dict[str, str]:
    """Fetch and shape the store list for an already configured client."""

    token = await client._async_token()
    session = client.session
    try:
        async with session.get(
            PAKNSAVE_STORES_URL,
            headers={**_BROWSER_HEADERS, "Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=25),
        ) as response:
            if response.status != 200:
                raise PriceError(f"Pak'nSave stores returned {response.status}")
            body = await response.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        raise PriceError("Pak'nSave store list failed") from err

    stores = body.get("stores") if isinstance(body, dict) else None
    if not isinstance(stores, list):
        raise PriceError("Pak'nSave store list was malformed")
    listing: dict[str, str] = {}
    for store in stores:
        if not isinstance(store, dict):
            continue
        store_id = store.get("id")
        store_name = store.get("name")
        if store_id and store_name:
            listing[str(store_id)] = str(store_name)
    return listing


def create_client(
    hass: HomeAssistant, chain: str, store_id: str | None
) -> PriceClient | None:
    """Build the price client for a profile, or None when pricing is off."""

    if chain == CHAIN_WOOLWORTHS:
        return WoolworthsPriceClient(hass, store_id)
    if chain == CHAIN_PAKNSAVE:
        return PakNSavePriceClient(hass, store_id)
    return None
