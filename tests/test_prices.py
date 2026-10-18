"""Price matching, specials detection, and price-lookup isolation."""

import asyncio
from types import SimpleNamespace

import pytest

import smart_shopping_route.price_coordinator as price_coordinator
from smart_shopping_route.prices import (
    CHAIN_PAKNSAVE,
    CHAIN_WOOLWORTHS,
    MIN_MATCH_SCORE,
    PakNSavePriceClient,
    PriceError,
    PriceQuote,
    WoolworthsPriceClient,
    best_match,
    create_client,
    match_score,
    search_term,
)


def quote(name: str, price: float, was: float | None = None) -> PriceQuote:
    return PriceQuote(
        chain=CHAIN_WOOLWORTHS,
        product_id=name,
        product_name=name,
        price=price,
        was_price=was,
    )


# --- matching ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("item", "product", "expected"),
    [
        ("Strawberries", "Woolworths Fresh Strawberry 250g", 1.0),
        ("Weetbix", "Weet-Bix Original Cereal 1.2kg", 1.0),
        ("Flora proactive buttery", "Flora ProActiv Buttery Spread 500g", 1.0),
        ("Bbq sauce", "Woolworths BBQ Sauce Squeeze 500ml", 1.0),
        ("Toilet paper", "Cotton Softs Toilet Paper 3 Ply 12pk", 1.0),
        ("Musashi drink", "Musashi Energy Drink Pineapple 500ml", 1.0),
        ("Eggs", "Henergy Cage Free Eggs Size 7 12pk", 1.0),
        ("Cheese slices", "Chesdale Cheese Slices Cheddar 200g", 1.0),
        ("Sausages", "Woolworths Beef Sausages 500g", 1.0),
    ],
)
def test_item_names_match_real_retailer_titles(item, product, expected) -> None:
    assert match_score(item, product) >= expected


def test_parenthetical_qualifier_is_part_of_the_match() -> None:
    """Regression: 'Tuna (olive oil)' must not resolve to a bottle of olive oil.

    Naive top-hit search returned "Pams Extra Virgin Olive Oil $13.99" for this
    item, overstating one basket by about twelve dollars.
    """

    assert match_score("Tuna (olive oil)", "Sealord Olive Oil Blend Tuna 185g") == 1.0
    assert match_score("Tuna (olive oil)", "Pams Extra Virgin Olive Oil 1L") < 1.0
    assert match_score("Tuna (olive oil)", "Pams Extra Virgin Olive Oil 1L") < (
        match_score("Tuna (olive oil)", "Sealord Olive Oil Blend Tuna 185g")
    )


def test_search_term_drops_the_parenthetical() -> None:
    assert search_term("Tuna (olive oil)") == "Tuna"
    assert search_term("Milk") == "Milk"
    assert search_term("(only parens)") == "(only parens)"


def test_search_term_can_keep_the_qualifier_for_a_retry() -> None:
    assert search_term("Tuna (olive oil)", keep_qualifier=True) == "Tuna olive oil"
    assert search_term("Milk", keep_qualifier=True) == "Milk"


def test_unrelated_product_scores_below_threshold() -> None:
    assert match_score("Kids vitamins", "Pams Frozen Peas 1kg") < MIN_MATCH_SCORE


def test_best_match_picks_the_closest_candidate() -> None:
    candidates = [
        quote("Pams Extra Virgin Olive Oil 1L", 13.99),
        quote("Sealord Olive Oil Blend Tuna 185g", 1.89),
        quote("Sealord Chunky Tuna In Water 185g", 1.89),
    ]

    result = best_match("Tuna (olive oil)", candidates)

    assert result is not None
    assert result.product_name == "Sealord Olive Oil Blend Tuna 185g"
    assert result.price == 1.89


def test_best_match_returns_none_rather_than_guessing() -> None:
    assert best_match("Kids vitamins", [quote("Pams Frozen Peas 1kg", 3.5)]) is None
    assert best_match("Milk", []) is None
    assert best_match("", [quote("Milk 2L", 4.0)]) is None


def test_retailer_relevance_order_breaks_a_tie() -> None:
    """Tokens cannot tell fresh from frozen; the retailer's order can.

    Searching "Strawberries" returns fresh punnets first, then frozen ones,
    strawberry wafers and puree, all of which contain the word.
    """

    candidates = [
        quote("Woolworths Fresh Strawberry 250g Punnet", 7.9),
        quote("Woolworths Frozen Strawberries 500g", 8.29),
        quote("Woolworths Creme Filled Strawberry Wafers 125g", 1.09),
    ]

    result = best_match("Strawberries", candidates)

    assert result is not None
    assert result.product_name == "Woolworths Fresh Strawberry 250g Punnet"


def test_higher_score_still_beats_earlier_relevance() -> None:
    candidates = [
        quote("Pams Extra Virgin Olive Oil 1L", 13.99),
        quote("Sealord Olive Oil Blend Tuna 185g", 1.89),
    ]

    result = best_match("Tuna (olive oil)", candidates)

    assert result is not None
    assert result.product_name == "Sealord Olive Oil Blend Tuna 185g"


def test_grocery_synonyms_match() -> None:
    """Pak'nSave sells "Toilet Tissue", not "Toilet Paper"."""

    assert (
        match_score("Toilet paper", "Pams White 3 Ply Long Roll Toilet Tissues") == 1.0
    )


# --- specials ---------------------------------------------------------------


def test_special_requires_a_higher_was_price() -> None:
    assert quote("Anchor Butter 500g", 8.5, 9.7).is_special is True
    assert quote("Anchor Butter 500g", 8.5, 9.7).savings == 1.2
    assert quote("Anchor Butter 500g", 8.5, None).is_special is False
    assert quote("Anchor Butter 500g", 8.5, 8.5).is_special is False
    assert quote("Anchor Butter 500g", 8.5, 8.5).savings == 0.0


def test_quote_serialises_for_sensor_attributes() -> None:
    payload = quote("Anchor Butter 500g", 8.5, 9.7).as_dict()

    assert payload["price"] == 8.5
    assert payload["was_price"] == 9.7
    assert payload["is_special"] is True


# --- clients ----------------------------------------------------------------


def test_disabled_chain_creates_no_client() -> None:
    assert create_client(SimpleNamespace(), "none", None) is None
    assert isinstance(
        create_client(SimpleNamespace(), CHAIN_WOOLWORTHS, None), WoolworthsPriceClient
    )


def test_paknsave_requires_a_store() -> None:
    with pytest.raises(PriceError):
        create_client(SimpleNamespace(), CHAIN_PAKNSAVE, "")


class FakeResponse:
    def __init__(self, status: int, body) -> None:
        self.status = status
        self._body = body

    async def json(self, **_kwargs):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class FakeSession:
    """Replay scripted responses and record the requests made."""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def woolworths_body(rows) -> dict:
    return {"data": {"My": {"products": {"results": rows}}}}


def test_woolworths_parses_prices_and_specials(monkeypatch) -> None:
    body = woolworths_body([
        {
            "sku": "123",
            "productName": "Anchor Butter 500g",
            "variants": [
                {
                    "variantPrice": {
                        "sellingPrice": 8.5,
                        "wasPrice": 9.7,
                        "isSpecial": True,
                    }
                }
            ],
        },
        {"sku": "456", "productName": "No Price Product", "variants": []},
        {"sku": None, "productName": "Ignored", "variants": []},
    ])
    client = WoolworthsPriceClient(SimpleNamespace())
    client._session = FakeSession([FakeResponse(200, body)])

    result = asyncio.run(client.async_lookup("butter"))

    assert result is not None
    assert result.product_name == "Anchor Butter 500g"
    assert result.price == 8.5
    assert result.was_price == 9.7
    assert result.is_special is True


def test_woolworths_rejects_a_failed_response() -> None:
    client = WoolworthsPriceClient(SimpleNamespace())
    client._session = FakeSession([FakeResponse(503, {})])

    with pytest.raises(PriceError):
        asyncio.run(client.async_lookup("butter"))


def test_paknsave_converts_cents_and_has_no_was_price() -> None:
    token = FakeResponse(200, {"access_token": "anon"})
    search = FakeResponse(
        200,
        {
            "products": [
                {
                    "productId": "p1",
                    "brand": "Pams",
                    "name": "Pure Butter 500g",
                    "singlePrice": {"price": 739},
                    "promotions": {
                        "priceValidityFreeFormText": "Valid until 18 October"
                    },
                }
            ]
        },
    )
    client = PakNSavePriceClient(SimpleNamespace(), "store-1")
    client._session = FakeSession([token, search])

    result = asyncio.run(client.async_lookup("butter"))

    assert result is not None
    assert result.price == 7.39
    assert result.product_name == "Pams Pure Butter 500g"
    assert result.valid_until == "Valid until 18 October"
    # The Foodstuffs API exposes no was-price, so nothing may be invented.
    assert result.was_price is None
    assert result.is_special is False


def test_paknsave_reuses_its_token() -> None:
    token = FakeResponse(200, {"access_token": "anon"})
    empty = {"products": []}
    client = PakNSavePriceClient(SimpleNamespace(), "store-1")
    client._session = FakeSession([
        token,
        FakeResponse(200, empty),
        FakeResponse(200, empty),
    ])

    asyncio.run(client.async_lookup("butter"))
    asyncio.run(client.async_lookup("milk"))

    token_calls = [c for c in client._session.calls if "get-current-user" in c["url"]]
    assert len(token_calls) == 1


# --- price coordinator ------------------------------------------------------


class FakeStore:
    def __init__(self) -> None:
        self.writes = 0

    async def async_save(self, _cache) -> None:
        self.writes += 1


def fake_entry():
    """Minimal config entry: the coordinator only registers an unload callback."""

    return SimpleNamespace(
        entry_id="e1", title="Shop", data={}, async_on_unload=lambda _cb: None
    )


def make_price_coordinator(monkeypatch, summaries, quotes_by_term):
    """Build a real PriceCoordinator with a scripted client."""

    calls: list[str] = []

    class Client:
        async def async_lookup(self, summary):
            calls.append(summary)
            return quotes_by_term.get(summary)

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: list(summaries)
    )
    coordinator._client = Client()
    return coordinator, calls


def test_each_summary_is_priced_once(monkeypatch) -> None:
    """Adding one item must not re-price the whole list."""

    summaries = ["Milk", "Bread"]
    coordinator, calls = make_price_coordinator(
        monkeypatch,
        summaries,
        {"Milk": quote("Milk 2L", 4.0), "Bread": quote("Bread 600g", 3.5)},
    )

    asyncio.run(coordinator._async_update_data())
    summaries.append("Eggs")
    asyncio.run(coordinator._async_update_data())

    assert calls == ["Milk", "Bread", "Eggs"]


def test_failed_lookup_is_not_retried_immediately(monkeypatch) -> None:
    coordinator, calls = make_price_coordinator(monkeypatch, ["Milk"], {})

    asyncio.run(coordinator._async_update_data())
    asyncio.run(coordinator._async_update_data())
    asyncio.run(coordinator._async_update_data())

    assert calls == ["Milk"]


def test_totals_and_specials(monkeypatch) -> None:
    coordinator, _calls = make_price_coordinator(
        monkeypatch,
        ["Butter", "Milk"],
        {
            "Butter": quote("Anchor Butter 500g", 8.5, 9.7),
            "Milk": quote("Milk 2L", 4.0),
        },
    )

    asyncio.run(coordinator._async_update_data())

    snapshot = coordinator.snapshot
    assert snapshot.total == 12.5
    assert [q.product_name for q in snapshot.specials] == ["Anchor Butter 500g"]
    assert snapshot.savings == 1.2
    assert coordinator.quote_for("Butter") is not None
    assert coordinator.quote_for("Unlisted") is None


def test_cheapest_seen_tracks_a_lower_price(monkeypatch) -> None:
    """Pak'nSave has no was-price, so history is the only specials signal."""

    coordinator, _calls = make_price_coordinator(
        monkeypatch, ["Butter"], {"Butter": quote("Butter 500g", 9.0)}
    )
    asyncio.run(coordinator._async_update_data())
    assert coordinator.cheapest_seen_for("Butter") == 9.0

    coordinator._snapshot.quotes.clear()
    coordinator._client.async_lookup = lambda _s: _return(quote("Butter 500g", 7.5))
    asyncio.run(coordinator._async_update_data())

    assert coordinator.cheapest_seen_for("Butter") == 7.5


async def _return(value):
    return value


def test_price_diagnostics_expose_counts_only(monkeypatch) -> None:
    coordinator, _calls = make_price_coordinator(
        monkeypatch, ["Butter"], {"Butter": quote("Anchor Butter 500g", 8.5)}
    )
    asyncio.run(coordinator._async_update_data())

    payload = coordinator.as_diagnostics()

    assert set(payload) == {
        "chain",
        "enabled",
        "priced_items",
        "unmatched_items",
        "scheduled_retries",
    }
    assert "Anchor" not in repr(payload)
    assert "Butter" not in repr(payload)


def test_disabled_pricing_makes_no_requests(monkeypatch) -> None:
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Milk"]
    )
    coordinator._client = None

    assert coordinator.enabled is False
    assert asyncio.run(coordinator._async_update_data()).quotes == {}
