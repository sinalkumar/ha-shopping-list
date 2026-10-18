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
class PriceQuote:
    """A matched price for one shopping item."""

    chain: str
    product_id: str
    product_name: str
    price: float
    was_price: float | None = None
    valid_until: str | None = None
    score: float = 0.0

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
        }


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

    async def async_lookup(self, summary: str) -> PriceQuote | None:
        """Return the best price for one shopping item, or None.

        A qualifier in brackets is tried only after the plain term fails, so
        the common case stays a single request.
        """

        candidates = await self.async_candidates(search_term(summary))
        match = best_match(summary, candidates)
        if match is not None:
            return match
        qualified = search_term(summary, keep_qualifier=True)
        if qualified == search_term(summary):
            return None
        return best_match(summary, await self.async_candidates(qualified))


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
            was = price_block.get("wasPrice")
            quotes.append(
                PriceQuote(
                    chain=self.chain,
                    product_id=str(row["sku"]),
                    product_name=str(row["productName"]),
                    price=float(selling),
                    was_price=float(was) if was else None,
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
            quotes.append(
                PriceQuote(
                    chain=self.chain,
                    product_id=str(product.get("productId") or name),
                    product_name=name,
                    price=round(float(single) / 100, 2),
                    was_price=None,
                    valid_until=str(validity) if validity else None,
                )
            )
        return quotes


async def async_list_paknsave_stores(
    hass: HomeAssistant, session: aiohttp.ClientSession | None = None
) -> dict[str, str]:
    """Return ``{store_id: store_name}`` for Pak'nSave.

    Used by the config flow so a profile can be pointed at a real store
    without hand-copying a UUID.
    """

    client = PakNSavePriceClient(hass, store_id="browse")
    if session is not None:
        client._session = session
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
