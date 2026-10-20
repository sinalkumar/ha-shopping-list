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
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

_LOGGER = logging.getLogger(__name__)

# An optional tie-break offered the shortlist when scoring finds nothing.
Ranker = Callable[[str, list["PriceQuote"]], Awaitable["PriceQuote | None"]]

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
# Retailer search returns candidates in relevance order and cuts them off at
# this size. Too small a pool silently loses qualifier variants that rank below
# the cut: "Tuna 185g" buries "Sealord Olive Oil Blend Tuna 185g" at position
# 12, so the item matched nothing at all. One request of 20 keeps them.
SEARCH_PAGE_SIZE = 20
# How many candidates an AI tie-break may be shown. Enough to cover the real
# variants of one product, small enough that the request stays cheap and the
# answer stays a choice rather than a search.
AI_SHORTLIST_MAX = 12
# How many value alternatives are shown beside a match. Five is enough to
# compare brands and pack formats without turning the row into a catalogue.
ALTERNATIVE_COUNT = 5
_SCORE_EPSILON = 1e-9
_VALUE_EPSILON = 1e-9

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
# A trailing "+" is part of the token, not punctuation: "50+" is an age
# formulation and "50" is a quantity, so collapsing them together loses the
# only thing that tells the two Centrum products apart.
_WORD_RE = re.compile(r"[^\W_]+\+?", re.UNICODE)
_PAREN_RE = re.compile(r"\([^)]*\)")
# Words that rule something out. Only a negated *quantity* is treated as an
# exclusion; see excluded_tokens().
_EXCLUDE_WORDS = frozenset({"not", "no", "without", "except", "minus"})

WOOLWORTHS_URL = "https://www.woolworths.co.nz/api/graphql"
PAKNSAVE_TOKEN_URL = "https://www.paknsave.co.nz/api/user/get-current-user"
PAKNSAVE_API = "https://api-prod.paknsave.co.nz"
PAKNSAVE_STORES_URL = f"{PAKNSAVE_API}/v1/edge/store"
PAKNSAVE_SEARCH_URL = f"{PAKNSAVE_API}/v1/edge/search/paginated/products"

_WOOLWORTHS_QUERY = """query ProductSearch($searchInput: CompositeSearchInput!) {
  My { products(searchInput: $searchInput) { results { __typename
    ... on ProductSummary { sku productName
      variants { unitOfMeasure variantKey variantPrice { sellingPrice wasPrice isSpecial } } } } } } }"""


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
    # "user" for a decision the household made, "auto" when an AI tie-break
    # settled an ambiguity. An automatic pin is a cached answer, so it may be
    # replaced or dropped when the shop changes; a user pin never is.
    source: str = "user"

    @property
    def is_auto(self) -> bool:
        return self.source == "auto"

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
            "source": self.source,
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
            source="auto" if data.get("source") == "auto" else "user",
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
    def unit_cost(self) -> float | None:
        """Numeric cost per base unit, for ordering quotes by value.

        The display string cannot be sorted ("$19.96/kg" < "$2.39/kg"), so the
        number is derived from the size the retailer states. None means the
        pack size is unknown, and such a quote cannot be compared by value.
        """

        size = size_from_text(self.size or self.product_name)
        if size is None or not math.isfinite(self.price) or self.price <= 0:
            return None
        _label, amount, _base_unit = size
        if not math.isfinite(amount) or amount <= 0:
            return None
        return self.price / amount

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


# Multipliers describe the whole purchase, not the first inner container.
_SIZE_IN_TEXT = re.compile(
    r"(?<![\w.])(?P<multipliers>(?:\d+\s*[x\u00d7]\s*)*)"
    r"(?P<amount>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>kg|g|ml|l|packs?|pk|each|ea|count|ct|tablets?|capsules?|gummies|rolls?|sheets?|s)\b"
    r"(?:\s*[x\u00d7]\s*(?P<suffix>\d+)\b)?",
    re.IGNORECASE,
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
    amount = float(match.group("amount"))
    multiplier = math.prod(
        int(n) for n in re.findall(r"\d+", match.group("multipliers"))
    )
    multiplier *= int(match.group("suffix") or 1)
    written = match.group("unit")
    unit = written.lower()
    total = amount * multiplier
    if not math.isfinite(total) or total <= 0:
        return None
    label = f"{match.group('amount')}{written}"
    if multiplier != 1:
        label = f"{multiplier} x {label}"
    if unit in _UNIT_LABELS:
        base_unit, factor = _UNIT_LABELS[unit]
        return (label, total * factor, base_unit)
    return (label, total, "sheets" if unit.startswith("sheet") else "ea")


def _format_unit_price(cost: Decimal, unit: str) -> str:
    """Avoid rounding a positive per-unit cost to an apparent free unit."""
    if unit == "sheets":
        cost *= 100
        unit = "100 sheets"
    rounded = cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded == 0:
        return f"<$0.01/{unit}"
    return f"${rounded:.2f}/{unit}"


def compute_unit_price(price: float, size: tuple[str, float, str] | None) -> str | None:
    """Return a per-unit price such as ``$6.66/kg``, or None when unknowable."""

    if size is None or not math.isfinite(price) or price <= 0:
        return None
    _label, amount, base_unit = size
    if not base_unit or not math.isfinite(amount) or amount <= 0:
        return None
    try:
        return _format_unit_price(Decimal(str(price)) / Decimal(str(amount)), base_unit)
    except InvalidOperation:
        return None


def _comparative_unit_price(comparative: dict[str, Any]) -> str | None:
    """Format a retailer's own comparative price, e.g. ``$1.48/100g``."""

    per_unit = comparative.get("pricePerUnit")
    quantity = comparative.get("unitQuantity")
    uom = comparative.get("unitQuantityUom")
    if per_unit is None or quantity is None or not uom:
        return None
    try:
        amount = float(quantity)
        cost = float(per_unit) / 100
        if (
            not math.isfinite(amount)
            or not math.isfinite(cost)
            or amount <= 0
            or cost <= 0
        ):
            return None
        unit = str(uom).lower()
        decimal_cost = Decimal(str(per_unit)) / 100
        if unit in _UNIT_LABELS:
            base, factor = _UNIT_LABELS[unit]
            return _format_unit_price(
                decimal_cost / (Decimal(str(amount)) * Decimal(str(factor))), base
            )
        if unit in ("sheet", "sheets"):
            return _format_unit_price(decimal_cost / Decimal(str(amount)), "sheets")
        basis = f"{amount:g}{uom}" if amount != 1 else str(uom)
        return _format_unit_price(decimal_cost, basis)
    except (TypeError, ValueError, InvalidOperation):
        return None


def _positive_price(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return price if math.isfinite(price) and price > 0 else None


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


def _size_tokens(summary: str) -> set[str]:
    """Tokens that spell out the pack size, e.g. {"8", "x", "270sheets"}."""

    size = size_from_text(summary)
    return set(_tokens(size[0])) if size else set()


def _negation_pairs(summary: str) -> list[tuple[str, str]]:
    """(negation word, ruled-out quantity) pairs, e.g. ("not", "50+")."""

    tokens = _tokens(summary)
    return [
        (token, tokens[index + 1])
        for index, token in enumerate(tokens[:-1])
        if token in _EXCLUDE_WORDS and any(ch.isdigit() for ch in tokens[index + 1])
    ]


def excluded_tokens(summary: str) -> set[str]:
    """Quantities the item explicitly rules out.

    "Centrum advance Adult 100pk (not 50+)" is a different product from the
    50+ formulation, and no similarity score can express "not": the word was
    counted as content, so writing it down voted for the wrong product. A
    negated word that carries a number is therefore a hard exclusion, because a
    pack size or an age formulation cannot be both. A negated word without a
    number ("no added sugar") is left to normal scoring, since retailer names
    state it positively and excluding it would discard the wanted product.
    """

    return {right for _left, right in _negation_pairs(summary)}


def _is_excluded(token: str, excluded: set[str]) -> bool:
    """True when a candidate token contradicts an excluded quantity.

    The trailing "+" is ignored here: "not 50" and "not 50+" both rule out the
    50+ formulation, so the exclusion must not depend on how it was typed.
    """

    bare = token.rstrip("+")
    return any(bare == other.rstrip("+") for other in excluded)


def match_score(summary: str, candidate: str) -> float:
    """Return the share of the item's words present in a candidate name.

    The pack size is left out of the share. best_match() already enforces it
    exactly, so counting it again only diluted the score: "100pk" is written on
    the pack, not in the product title, which pushed a correct match below the
    threshold while a wrong variant stayed above it.
    """

    item_tokens = _tokens(summary)
    if not item_tokens:
        return 0.0
    # A ruled-out quantity and the word that rules it out are instructions, not
    # product words: keeping them in the share let "not 50+" dilute the score
    # and, worse, count "50" as matched against the product it excluded.
    ignored = _size_tokens(summary)
    for negation, quantity in _negation_pairs(summary):
        ignored.add(negation)
        ignored.add(quantity)
    content = [token for token in item_tokens if token not in ignored]
    if content:
        item_tokens = content
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


def size_free_term(summary: str) -> str:
    """Return the item without its pack size.

    A pack count is a constraint, not a search word: retailers keep it on the
    box, so searching it excludes the right product. "Centrum advance Adult
    100pk" returns only the 50+ box, while "Centrum advance Adult" returns both.
    The size is still enforced exactly during matching, so dropping it here
    loosens the query without loosening the answer.
    """

    match = _SIZE_IN_TEXT.search(summary or "")
    if match is None:
        return ""
    stripped = (summary[: match.start()] + " " + summary[match.end() :]).strip()
    return search_term(stripped)


def best_match(summary: str, candidates: list[PriceQuote]) -> PriceQuote | None:
    """Pick the closest candidate, or None when nothing is close enough.

    The score acts as a filter, and the retailer's own relevance order breaks
    ties. That ordering carries signal the tokens do not: for "Strawberries" it
    puts fresh punnets above frozen ones, chocolate bites, and wafers, all of
    which contain the word.
    """

    best: PriceQuote | None = None
    best_score = 0.0
    excluded = excluded_tokens(summary)
    wanted_size = size_from_text(summary)
    for candidate in candidates:
        if not _acceptable(summary, candidate, excluded):
            continue
        score = match_score(summary, candidate.product_name)
        if score < MIN_MATCH_SCORE:
            continue
        if best is not None and math.isclose(score, best_score, abs_tol=_SCORE_EPSILON):
            # Equally relevant and the same pack means the same product from a
            # different brand, so buy the one that is cheaper per unit. Only a
            # stated pack size makes that comparison meaningful: without one,
            # "Strawberries" scores equally for a punnet and for strawberry
            # wafers, and the cheaper per gram would be the wrong purchase. A
            # tie that value cannot settle keeps the retailer's own order.
            if wanted_size is None or not _cheaper_per_unit(candidate, best):
                continue
        elif score <= best_score:
            continue
        best_score = score
        best = replace(candidate, score=score)
    return best


def _cheaper_per_unit(candidate: PriceQuote, incumbent: PriceQuote) -> bool:
    """True when ``candidate`` is the better value of two equally close matches."""

    new = candidate.unit_cost
    old = incumbent.unit_cost
    if new is None:
        return False
    if old is None:
        return True
    return new < old - _VALUE_EPSILON


def alternatives(
    summary: str, candidates: list[PriceQuote], limit: int = ALTERNATIVE_COUNT
) -> list[PriceQuote]:
    """Return the best-value alternatives for an item, cheapest per unit first.

    Only candidates that satisfy the same hard constraints as a match are
    offered, so this never suggests a different pack size or a ruled-out
    variant. They must also be the same thing: a pack size alone does not make
    a product a substitute, or "Tuna 185g (olive oil)" would be offered Lemon
    Pepper and Sweet Thai Chilli -- the flavours its own qualifier excludes --
    and an unpriced vitamin would be offered unrelated gummies that happen to
    be 100pk. The relevance floor is the same one a match must clear.

    Quotes whose size is unknown cannot be compared by value and are left out
    rather than guessed at.
    """

    if size_from_text(summary) is None:
        # Nothing to compare: a per-unit price of a differently sized product is
        # not an alternative, it is a different purchase.
        return []
    excluded = excluded_tokens(summary)
    seen: set[str] = set()
    priced: list[PriceQuote] = []
    for candidate in candidates:
        if candidate.product_id in seen or not _acceptable(
            summary, candidate, excluded
        ):
            continue
        if candidate.unit_cost is None:
            continue
        if match_score(summary, candidate.product_name) < MIN_MATCH_SCORE:
            continue
        seen.add(candidate.product_id)
        priced.append(candidate)
    priced.sort(key=lambda quote: (quote.unit_cost, quote.price, quote.product_name))
    return priced[:limit]


def _acceptable(
    summary: str, candidate: PriceQuote, excluded: set[str] | None = None
) -> bool:
    """Apply the constraints a similarity score must never override."""

    wanted_size = size_from_text(summary)
    candidate_size = size_from_text(candidate.size or candidate.product_name)
    # A size the retailer states and that contradicts the item is fatal. A size
    # it does not state is not evidence of a contradiction: pack counts are
    # printed on the box, not in the title, so "Centrum Advance Adults" carries
    # no "100" even though that is the pack the household means. Rejecting on
    # silence left correct products unpriced; the quote simply reports no size.
    if (
        wanted_size
        and candidate_size
        and (
            wanted_size[2] != candidate_size[2]
            or not math.isclose(wanted_size[1], candidate_size[1], rel_tol=1e-6)
        )
    ):
        return False
    if excluded is None:
        excluded = excluded_tokens(summary)
    if excluded:
        written = _tokens(candidate.product_name) + _tokens(candidate.size or "")
        if any(_is_excluded(token, excluded) for token in written):
            return False
    return True


def shortlist(
    summary: str, candidates: list[PriceQuote], limit: int = AI_SHORTLIST_MAX
) -> list[PriceQuote]:
    """Rank the candidates a reviewer (human or model) should see.

    Only candidates that already satisfy the hard constraints are offered, so a
    tie-break can choose between real options but cannot smuggle in a wrong pack
    size or a ruled-out variant. Retailer relevance order survives equal scores
    because the sort is stable.
    """

    excluded = excluded_tokens(summary)
    scored = [
        (match_score(summary, candidate.product_name), candidate)
        for candidate in candidates
        if _acceptable(summary, candidate, excluded)
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [replace(candidate, score=score) for score, candidate in scored[:limit]]


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
        self,
        summary: str,
        pin: ProductPin | None = None,
        *,
        ranker: Ranker | None = None,
        alternatives_out: list[PriceQuote] | None = None,
    ) -> PriceQuote | None:
        """Return the best price for one shopping item, or None.

        A user pin takes precedence over automatic matching: its query is used
        as the search term and its product ID, when set, must match exactly. An
        exact pin that cannot be found returns nothing rather than falling back
        to a guess, because a pin is a decision, not a hint.

        Without a pin, a qualifier in brackets is tried only after the plain
        term fails, so the common case stays a single request. When scoring
        still finds nothing, ``ranker`` is offered the candidates that already
        satisfied the hard constraints; it may only pick one of those or
        decline, so a tie-break can never override a pack size or a ruled-out
        variant.
        """

        def matched(
            match: PriceQuote | None, pool: list[PriceQuote]
        ) -> PriceQuote | None:
            # The alternatives answer the question the match does not: whether
            # the product that won on relevance was also the best value on that
            # shelf. They are collected from the same pool the match came from,
            # so the two can never disagree about what was on offer.
            if alternatives_out is not None and match is not None:
                alternatives_out.extend(alternatives(summary, pool))
            return match

        if pin is not None and pin.product_id:
            pinned = await self.async_candidates(self._pin_term(pin, summary))
            return matched(self._exact(pinned, pin), pinned)

        target = pin.match_target if pin and pin.match_target else summary
        candidates = await self.async_candidates(self._pin_term(pin, summary))
        match = best_match(target, candidates)
        if match is not None:
            return matched(match, candidates)
        if pin is not None and pin.match_target:
            return None
        # Each retry widens the query rather than the answer: the qualifier is
        # re-added because the plain noun may be too generic, and the pack size
        # is dropped because it is printed on the box, not in the title. The
        # matching constraints still apply to every candidate either way.
        tried = {self._pin_term(pin, summary), search_term(summary)}
        for retry in (
            search_term(summary, keep_qualifier=True),
            size_free_term(summary),
        ):
            if not retry or retry in tried:
                continue
            tried.add(retry)
            candidates = candidates + await self.async_candidates(retry)
            match = best_match(summary, candidates)
            if match is not None:
                return matched(match, candidates)
        if alternatives_out is not None:
            alternatives_out.extend(alternatives(summary, candidates))
        if ranker is None:
            return None
        return await ranker(summary, shortlist(summary, candidates))

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
            # A row with multiple unlabelled variants cannot safely associate
            # the title's pack size with variants[0]'s price.
            if len(variants) != 1:
                continue
            price_block = (variants[0] or {}).get("variantPrice")
            if not price_block:
                continue
            selling = _positive_price(price_block.get("sellingPrice"))
            if selling is None:
                continue
            name = str(row["productName"])
            was = _positive_price(price_block.get("wasPrice"))
            quotes.append(
                PriceQuote(
                    chain=self.chain,
                    product_id=str(row["sku"]),
                    product_name=name,
                    price=float(selling),
                    was_price=float(was) if was else None,
                    size=(parsed[0] if (parsed := size_from_text(name)) else None),
                    unit_price=compute_unit_price(float(selling), size_from_text(name))
                    or (
                        _format_unit_price(Decimal(str(selling)), "kg")
                        if variants[0].get("unitOfMeasure") == "KG"
                        else None
                    ),
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
            single = _positive_price((product.get("singlePrice") or {}).get("price"))
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
            # displayName is the authoritative pack metadata (e.g. '500g').
            # Rounded comparative prices cannot determine an exact pack size.
            parsed_size = size_from_text(
                str(product.get("displayName") or "")
            ) or size_from_text(name)
            size_label = parsed_size[0] if parsed_size else None
            unit_price = compute_unit_price(
                shelf_price, parsed_size
            ) or _comparative_unit_price(comparative)
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
