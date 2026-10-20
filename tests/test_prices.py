"""Price matching, specials detection, and price-lookup isolation."""

import asyncio
import time
from types import SimpleNamespace

import pytest

import smart_shopping_route.price_coordinator as price_coordinator
from smart_shopping_route import ai_match as prices_ai_match
from smart_shopping_route.const import (
    PRICE_REFRESH_HOURS,
    PRICE_STALE_AFTER_HOURS,
)
from smart_shopping_route.prices import (
    ALTERNATIVE_COUNT,
    CHAIN_PAKNSAVE,
    CHAIN_WOOLWORTHS,
    MIN_MATCH_SCORE,
    PakNSavePriceClient,
    PriceClient,
    PriceError,
    PriceQuote,
    ProductPin,
    WoolworthsPriceClient,
    alternatives,
    best_match,
    create_client,
    excluded_tokens,
    match_score,
    search_term,
    shortlist,
    size_free_term,
)
from smart_shopping_route.prices import _tokens as prices_tokens


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
        entry_id="e1",
        title="Shop",
        data={},
        async_on_unload=lambda _cb: None,
        pref_disable_polling=False,
    )


def make_price_coordinator(monkeypatch, summaries, quotes_by_term):
    """Build a real PriceCoordinator with a scripted client."""

    calls: list[str] = []

    class Client:
        async def async_lookup(self, summary, pin=None, **_kwargs):
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


def test_a_manual_refresh_retries_a_missed_item(monkeypatch) -> None:
    """A miss backs off for an hour, but a refresh is the household asking now.

    Without this, pressing Refresh within that hour silently skipped the very
    item the household was looking at, which read as the button doing nothing.
    """

    coordinator, calls = make_price_coordinator(monkeypatch, ["Milk"], {})

    asyncio.run(coordinator._async_update_data())
    assert calls == ["Milk"]
    assert "milk" in coordinator._backoff_until

    coordinator._force_full = True
    asyncio.run(coordinator._async_update_data())

    assert calls == ["Milk", "Milk"]


def test_a_missed_item_is_retried_on_the_next_interval(monkeypatch) -> None:
    """The slow interval heals a miss once the retailer's catalogue changes."""

    coordinator, calls = make_price_coordinator(monkeypatch, ["Milk"], {})

    asyncio.run(coordinator._async_update_data())
    coordinator._backoff_until.clear()  # the 12h interval has elapsed
    coordinator._force_full = True
    asyncio.run(coordinator._async_update_data())

    assert calls == ["Milk", "Milk"]


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
    assert [quote.product_name for _key, quote in snapshot.specials] == [
        "Anchor Butter 500g"
    ]
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
    coordinator._client.async_lookup = lambda _s, _p=None, **_k: _return(
        quote("Butter 500g", 7.5)
    )
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
        "pinned_items",
        "ai_match",
        "automatic_pins",
        "items_with_history",
        "grocer_history",
        "grocer_mirror_bytes",
        "refresh_hours",
        "stale_after_hours",
        "weekly_refresh",
    }
    assert payload["refresh_hours"] == PRICE_REFRESH_HOURS
    assert payload["stale_after_hours"] == PRICE_STALE_AFTER_HOURS
    assert payload["weekly_refresh"] == {
        "weekday": 0,
        "hour": 6,
        "scheduled": False,
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


# --- basket accounting ------------------------------------------------------


def test_duplicate_summaries_count_at_their_real_quantity(monkeypatch) -> None:
    """Two 'Milk' entries are two purchases, not one."""

    coordinator, calls = make_price_coordinator(
        monkeypatch,
        ["Milk", "Milk", "Bread"],
        {"Milk": quote("Milk 2L", 4.0), "Bread": quote("Bread 600g", 3.5)},
    )

    asyncio.run(coordinator._async_update_data())

    assert coordinator.snapshot.total == 11.5
    assert coordinator.snapshot.priced_count == 3
    # One request per distinct summary, not per item.
    assert sorted(calls) == ["Bread", "Milk"]


def test_removed_items_stop_counting_and_are_pruned(monkeypatch) -> None:
    summaries = ["Milk", "Bread"]
    coordinator, _calls = make_price_coordinator(
        monkeypatch,
        summaries,
        {"Milk": quote("Milk 2L", 4.0), "Bread": quote("Bread 600g", 3.5)},
    )
    asyncio.run(coordinator._async_update_data())
    assert coordinator.snapshot.total == 7.5

    summaries.remove("Bread")
    asyncio.run(coordinator._async_update_data())

    assert coordinator.snapshot.total == 4.0
    assert coordinator.snapshot.quotes.keys() == {"milk"}


def test_completed_items_stop_counting(monkeypatch) -> None:
    """The coordinator only asks for incomplete items, so completed ones drop out."""

    summaries = ["Milk", "Bread"]
    coordinator, _calls = make_price_coordinator(
        monkeypatch,
        summaries,
        {"Milk": quote("Milk 2L", 4.0), "Bread": quote("Bread 600g", 3.5)},
    )
    asyncio.run(coordinator._async_update_data())

    summaries.remove("Milk")  # what the route coordinator reports once completed
    asyncio.run(coordinator._async_update_data())

    assert coordinator.snapshot.total == 3.5
    assert coordinator.quote_for("Milk") is None


def test_specials_savings_scale_with_quantity(monkeypatch) -> None:
    coordinator, _calls = make_price_coordinator(
        monkeypatch,
        ["Butter", "Butter"],
        {"Butter": quote("Anchor Butter 500g", 8.5, 9.7)},
    )

    asyncio.run(coordinator._async_update_data())

    assert coordinator.snapshot.savings == 2.4
    assert len(coordinator.snapshot.specials) == 1


# --- failure reporting ------------------------------------------------------


def test_all_transport_failures_raise_update_failed(monkeypatch) -> None:
    """An outage must be visible, not silently reported as zero failures."""

    from homeassistant.helpers.update_coordinator import UpdateFailed

    class FailingClient:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            raise PriceError("down")

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Milk"]
    )
    coordinator._client = FailingClient()

    with pytest.raises(UpdateFailed):
        asyncio.run(coordinator._async_update_data())

    assert coordinator.as_diagnostics()["scheduled_retries"] == 1


def test_partial_transport_failure_does_not_raise(monkeypatch) -> None:
    class MixedClient:
        async def async_lookup(self, summary, pin=None, **_kwargs):
            if summary == "Milk":
                raise PriceError("down")
            return quote("Bread 600g", 3.5)

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Milk", "Bread"]
    )
    coordinator._client = MixedClient()

    snapshot = asyncio.run(coordinator._async_update_data())

    assert snapshot.total == 3.5


def test_no_match_is_not_a_transport_failure(monkeypatch) -> None:
    """An unstocked item is a normal outcome, not a degraded connection."""

    coordinator, _calls = make_price_coordinator(monkeypatch, ["Kids vitamins"], {})

    snapshot = asyncio.run(coordinator._async_update_data())

    assert snapshot.total == 0.0
    assert snapshot.unmatched_count == 1
    assert coordinator.as_diagnostics()["unmatched_items"] == 1


# --- history ----------------------------------------------------------------


def test_previous_price_records_the_old_value(monkeypatch) -> None:
    """Regression: the new price was being recorded as the previous price."""

    coordinator, _calls = make_price_coordinator(
        monkeypatch, ["Butter"], {"Butter": quote("Butter 500g", 9.0)}
    )
    asyncio.run(coordinator._async_update_data())
    assert coordinator.cheapest_seen_for("Butter") == 9.0

    coordinator._client.async_lookup = lambda _s, _p=None, **_k: _return(
        quote("Butter 500g", 7.5)
    )
    asyncio.run(coordinator.async_refresh())

    assert coordinator.snapshot.reference_price("butter") == 9.0
    assert coordinator.cheapest_seen_for("Butter") == 7.5


# --- lifecycle --------------------------------------------------------------


def test_setup_registers_an_interval_listener(monkeypatch) -> None:
    """Without a listener the interval refresh never runs."""

    monkeypatch.setattr(
        price_coordinator, "create_client", lambda *_a, **_k: SimpleNamespace()
    )
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(loop=asyncio.new_event_loop()),
        fake_entry(),
        summaries=lambda: ["Milk"],
    )

    coordinator.async_setup()

    assert coordinator._unsub_listener is not None


def test_shutdown_cancels_an_in_flight_refresh(monkeypatch) -> None:
    """A slow retailer must not mutate the snapshot after unload."""

    started = asyncio.Event()
    cancelled = False

    class SlowClient:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            nonlocal cancelled
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled = True
                raise
            return quote("Milk 2L", 4.0)

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Milk"]
    )
    coordinator._client = SlowClient()

    async def scenario() -> None:
        refresh = asyncio.create_task(coordinator.async_refresh())
        await started.wait()
        await coordinator.async_shutdown()
        await asyncio.gather(refresh, return_exceptions=True)

    asyncio.run(scenario())

    assert cancelled is True
    assert coordinator.snapshot.quotes == {}


def test_refresh_after_shutdown_does_nothing(monkeypatch) -> None:
    coordinator, calls = make_price_coordinator(
        monkeypatch, ["Milk"], {"Milk": quote("Milk 2L", 4.0)}
    )
    asyncio.run(coordinator.async_shutdown())

    asyncio.run(coordinator.async_refresh())

    assert calls == []


def test_forced_refresh_reprices_existing_items(monkeypatch) -> None:
    """A cached quote must still be refreshed on the slow interval.

    The interval path sets the same flag this test sets, so a cached quote
    cannot live forever while prices change at the retailer.
    """

    prices = {"Butter": 9.0}

    class MovingClient:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            return quote("Butter 500g", prices["Butter"])

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(is_stopping=lambda: False, state=None),
        fake_entry(),
        summaries=lambda: ["Butter"],
    )
    coordinator._client = MovingClient()

    asyncio.run(coordinator._async_update_data())
    assert coordinator.snapshot.total == 9.0

    prices["Butter"] = 7.5
    coordinator._force_full = True
    asyncio.run(coordinator._async_update_data())

    assert coordinator.snapshot.total == 7.5


def test_source_push_does_not_repice_known_items(monkeypatch) -> None:
    """The incremental path stays one request per new item."""

    coordinator, calls = make_price_coordinator(
        monkeypatch, ["Butter"], {"Butter": quote("Butter 500g", 9.0)}
    )
    asyncio.run(coordinator._async_update_data())
    asyncio.run(coordinator._async_update_data())

    assert calls == ["Butter"]


# --- pins -------------------------------------------------------------------


def test_pin_query_reaches_a_product_the_item_name_cannot(monkeypatch) -> None:
    """A household shorthand has to be pinnable: 'Kids vitamins' is Multivitamin Gummies."""

    searched: list[str] = []

    class Client:
        async def async_lookup(self, summary, pin=None, **_kwargs):
            searched.append(pin.match_target if pin else summary)
            return quote(
                "Example Brand Kids Multivitamin Gummies 110 Pack",
                19.99,
            )

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Kids vitamins"]
    )
    coordinator._client = Client()

    asyncio.run(
        coordinator.async_pin("Kids vitamins", query="Kids Multivitamin Gummies")
    )

    assert searched == ["Kids Multivitamin Gummies"]
    assert coordinator.snapshot.total == 19.99
    assert coordinator.pin_for("Kids vitamins") is not None


def test_pin_is_persisted_and_reloaded(monkeypatch) -> None:
    from smart_shopping_route.prices import ProductPin

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Kids vitamins"]
    )
    coordinator._client = None
    asyncio.run(
        coordinator.async_pin(
            "Kids vitamins",
            query="Kids Multivitamin Gummies",
            product_id="1234567",
            product_name="Example Brand Kids Multivitamin Gummies 110 Pack",
        )
    )

    store = price_coordinator.PriceStore.instances[-1]
    assert store.saved
    pins = store.saved[-1]["pins"]
    assert pins["kids vitamins"]["product_id"] == "1234567"

    restored = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Kids vitamins"]
    )
    restored._price_store.initial = store.saved[-1]
    asyncio.run(restored.async_load())

    pin = restored.pin_for("Kids vitamins")
    assert pin == ProductPin(
        chain="",
        query="Kids Multivitamin Gummies",
        product_id="1234567",
        product_name="Example Brand Kids Multivitamin Gummies 110 Pack",
    )


def test_exact_pin_never_falls_back_to_a_near_miss() -> None:
    """A pin is a decision: if the exact product is gone, show nothing."""

    from smart_shopping_route.prices import ProductPin, WoolworthsPriceClient

    client = WoolworthsPriceClient(SimpleNamespace())

    async def candidates(_term):
        return [quote("Example Brand Kids Multivitamin Gummies 30 Pack", 9.99)]

    client.async_candidates = candidates  # type: ignore[method-assign]
    pin = ProductPin(
        chain="woolworths", query="Multivitamin Gummies", product_id="1234567"
    )

    assert asyncio.run(client.async_lookup("Kids vitamins", pin)) is None


def test_exact_pin_selects_the_pinned_product() -> None:
    from smart_shopping_route.prices import ProductPin, WoolworthsPriceClient

    client = WoolworthsPriceClient(SimpleNamespace())

    async def candidates(_term):
        small = PriceQuote(
            chain="woolworths",
            product_id="7654321",
            product_name="Example Brand Kids Multivitamin Gummies 30 Pack",
            price=9.99,
        )
        big = PriceQuote(
            chain="woolworths",
            product_id="1234567",
            product_name="Example Brand Kids Multivitamin Gummies 110 Pack",
            price=19.99,
        )
        return [small, big]

    client.async_candidates = candidates  # type: ignore[method-assign]
    pin = ProductPin(
        chain="woolworths", query="Multivitamin Gummies", product_id="1234567"
    )

    result = asyncio.run(client.async_lookup("Kids vitamins", pin))

    assert result is not None
    assert result.price == 19.99


def test_unpin_forgets_the_decision(monkeypatch) -> None:
    coordinator, _calls = make_price_coordinator(monkeypatch, ["Milk"], {})
    coordinator._client = None
    asyncio.run(coordinator.async_pin("Milk", query="Milk 2L"))

    asyncio.run(coordinator.async_unpin("Milk"))

    assert coordinator.pin_for("Milk") is None


# --- price drops from history ----------------------------------------------


def test_drop_is_reported_when_a_price_falls(monkeypatch) -> None:
    """Pak'nSave publishes no was-price, so our own history is the signal."""

    prices = {"Butter": 9.0}

    class Client:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            return quote("Pams Butter 500g", prices["Butter"])

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Butter"]
    )
    coordinator._client = Client()

    asyncio.run(coordinator._async_update_data())
    prices["Butter"] = 7.5
    coordinator._force_full = True
    asyncio.run(coordinator._async_update_data())

    snapshot = coordinator.snapshot
    assert snapshot.drop_for("butter") == 1.5
    assert [(key, drop) for key, _q, drop in snapshot.drops] == [("butter", 1.5)]


def test_no_drop_reported_when_price_is_unchanged_or_rises(monkeypatch) -> None:
    prices = {"Butter": 9.0}

    class Client:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            return quote("Pams Butter 500g", prices["Butter"])

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Butter"]
    )
    coordinator._client = Client()

    asyncio.run(coordinator._async_update_data())
    coordinator._force_full = True
    asyncio.run(coordinator._async_update_data())
    assert coordinator.snapshot.drop_for("butter") is None

    prices["Butter"] = 12.0
    coordinator._force_full = True
    asyncio.run(coordinator._async_update_data())
    assert coordinator.snapshot.drop_for("butter") is None


def test_a_real_special_is_not_double_counted_as_a_drop(monkeypatch) -> None:
    prices = {"Butter": 9.7}

    class Client:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            return quote("Anchor Butter 500g", 8.5, was=prices["Butter"])

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Butter"]
    )
    coordinator._client = Client()

    asyncio.run(coordinator._async_update_data())
    prices["Butter"] = 9.7
    coordinator._force_full = True
    asyncio.run(coordinator._async_update_data())

    snapshot = coordinator.snapshot
    assert len(snapshot.specials) == 1
    assert snapshot.drop_for("butter") is None


def test_unexpected_error_is_logged_once_per_cycle(caplog) -> None:
    """A changed response shape must not warn once per item per refresh."""

    import logging

    class Broken:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            raise RuntimeError("shape changed")

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["A", "B", "C"]
    )
    coordinator._client = Broken()

    with caplog.at_level(logging.WARNING):
        asyncio.run(coordinator._async_update_data())

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "3 item(s)" in warnings[0].getMessage()


def test_unexpected_error_backs_off(monkeypatch) -> None:
    """Otherwise a persistently broken response is retried on every push."""

    calls: list[str] = []

    class Broken:
        async def async_lookup(self, summary, _pin=None, **_kwargs):
            calls.append(summary)
            raise RuntimeError("shape changed")

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Milk"]
    )
    coordinator._client = Broken()

    for _ in range(3):
        asyncio.run(coordinator._async_update_data())

    assert calls == ["Milk"]


def test_price_history_is_bounded(monkeypatch) -> None:
    """History is persisted, so it must not grow without limit."""

    from smart_shopping_route.const import PRICE_HISTORY_MAX

    summaries = ["Milk"]

    class Client:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            return quote("Milk 2L", 4.0)

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: list(summaries)
    )
    coordinator._client = Client()
    coordinator._snapshot.history = {
        f"old item {i}": [price_coordinator.PricePoint(0.0, 1.0)]
        for i in range(PRICE_HISTORY_MAX + 50)
    }

    asyncio.run(coordinator._async_update_data())

    assert len(coordinator.snapshot.history) <= PRICE_HISTORY_MAX


def test_pin_from_another_chain_is_discarded_on_load() -> None:
    """A pin is per retailer: reusing it after a reconfigure prices the wrong product."""

    entry = fake_entry()
    entry.data = {"price_chain": "paknsave"}
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), entry, summaries=lambda: ["Kids vitamins"]
    )
    coordinator._price_store.initial = {
        "pins": {
            "kids vitamins": {
                "chain": "woolworths",
                "query": "Kids Multivitamin Gummies",
                "product_id": "1234567",
                "product_name": "Example Brand Kids Multivitamin Gummies 110 Pack",
            }
        }
    }

    asyncio.run(coordinator.async_load())

    assert coordinator.pin_for("Kids vitamins") is None


def test_pin_for_the_same_chain_survives_load() -> None:
    entry = fake_entry()
    entry.data = {"price_chain": "woolworths"}
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), entry, summaries=lambda: ["Kids vitamins"]
    )
    coordinator._price_store.initial = {
        "pins": {
            "kids vitamins": {
                "chain": "woolworths",
                "query": "Kids Multivitamin Gummies",
                "product_id": "1234567",
            }
        }
    }

    asyncio.run(coordinator.async_load())

    pin = coordinator.pin_for("Kids vitamins")
    assert pin is not None
    assert pin.product_id == "1234567"


def test_drop_is_reported_only_in_the_refresh_that_observed_it(monkeypatch) -> None:
    """A price that stays low must not report a drop on every later refresh."""

    prices = {"Butter": 9.0}

    class Client:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            return quote("Pams Butter 500g", prices["Butter"])

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Butter"]
    )
    coordinator._client = Client()
    asyncio.run(coordinator._async_update_data())

    prices["Butter"] = 7.5
    coordinator._force_full = True
    asyncio.run(coordinator._async_update_data())
    assert coordinator.snapshot.drop_for("butter") == 1.5

    # Next refresh: unchanged price, so no drop event.
    coordinator._force_full = True
    asyncio.run(coordinator._async_update_data())
    assert coordinator.snapshot.drop_for("butter") is None
    assert coordinator.snapshot.drops == []


def test_history_is_persisted_after_a_refresh() -> None:
    """HA shutdown does not unload entries, so history must be written eagerly."""

    class Client:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            return quote("Milk 2L", 4.0)

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Milk"]
    )
    coordinator._client = Client()

    asyncio.run(coordinator.async_refresh())

    saved = coordinator._price_store.saved
    assert saved and saved[-1]["history"]["milk"]["points"][-1][1] == 4.0


def test_a_request_during_a_refresh_runs_again_afterwards() -> None:
    """A pin change or a new item must not wait for the next interval."""

    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    class SlowClient:
        async def async_lookup(self, summary, _pin=None, **_kwargs):
            calls.append(summary)
            if len(calls) == 1:
                started.set()
                await release.wait()
            return quote("Milk 2L", 4.0)

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Milk"]
    )
    coordinator._client = SlowClient()

    async def scenario() -> None:
        first = asyncio.create_task(coordinator.async_refresh())
        await started.wait()
        # A second request arrives while the first is in flight.
        second = asyncio.create_task(coordinator.async_refresh(force=False))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())

    assert coordinator._rerun_requested is False


def test_the_real_interval_path_reprices_and_notifies() -> None:
    """The scheduled interval path must re-price AND re-render entities.

    It calls the framework's private refresh, bypassing both our public
    async_refresh and RouteCoordinator's notification helper, so this covers the
    exact path a timer takes.
    """

    notified: list[bool] = []

    class Client:
        def __init__(self) -> None:
            self.price = 9.0

        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            return quote("Butter 500g", self.price)

    entry = fake_entry()
    entry.data = {"price_chain": "woolworths"}
    loop = asyncio.new_event_loop()
    coordinator = price_coordinator.PriceCoordinator(
        # is_stopping is a plain bool here, as the real property is; a truthy
        # callable would make the framework skip a scheduled refresh.
        SimpleNamespace(loop=loop, is_stopping=False),
        entry,
        summaries=lambda: ["Butter"],
    )
    coordinator._client = Client()
    coordinator.async_set_update_callback(lambda: notified.append(True))
    coordinator._unsub_listener = coordinator.async_add_listener(
        coordinator._async_handle_update_done
    )
    asyncio.run(coordinator._async_refresh_guarded())
    assert coordinator.snapshot.total == 9.0

    coordinator._client.price = 7.0
    asyncio.run(coordinator._handle_refresh_interval())

    assert coordinator.snapshot.total == 7.0
    assert coordinator.snapshot.drop_for("butter") == 2.0
    assert len(notified) == 2


def test_interval_refresh_notifies_entities() -> None:
    """The interval must re-render entities, not just update the snapshot.

    It bypasses the route coordinator's own notification path, so without the
    listener forwarding, price sensors and item descriptions stay stale.
    """

    notified: list[bool] = []

    class Client:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            return quote("Milk 2L", 4.0)

    entry = fake_entry()
    entry.data = {"price_chain": "woolworths"}
    loop = asyncio.new_event_loop()
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(loop=loop, is_stopping=lambda: False),
        entry,
        summaries=lambda: ["Milk"],
    )
    coordinator._client = Client()
    coordinator.async_set_update_callback(lambda: notified.append(True))
    # Register the listener the way async_setup does, without a real retailer.
    coordinator._unsub_listener = coordinator.async_add_listener(
        coordinator._async_handle_update_done
    )

    asyncio.run(coordinator._async_refresh_guarded())

    assert notified == [True]


def test_update_callback_is_not_called_after_shutdown() -> None:
    notified: list[bool] = []
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Milk"]
    )
    coordinator.async_set_update_callback(lambda: notified.append(True))
    asyncio.run(coordinator.async_shutdown())

    coordinator._async_handle_update_done()

    assert notified == []


def test_pins_are_bounded() -> None:
    """Pins persist forever by design, so they still need a cap."""

    from smart_shopping_route.const import PRICE_PIN_MAX

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Milk"]
    )
    coordinator._client = None
    for i in range(PRICE_PIN_MAX + 20):
        coordinator._pins[f"item {i}"] = ProductPin(
            chain="", query=f"query {i}", product_id=str(i)
        )

    asyncio.run(coordinator.async_pin("Milk", query="Milk 2L"))

    assert len(coordinator._pins) <= PRICE_PIN_MAX
    # The newest pin must survive the trim.
    assert coordinator.pin_for("Milk") is not None


def test_async_setup_registers_a_listener_that_notifies(monkeypatch) -> None:
    """Go through async_setup itself, so what it registers is what is tested.

    Registering the listener by hand in the test would hide the original bug,
    where async_setup registered a no-op and interval refreshes never reached
    entities.
    """

    notified: list[bool] = []

    class Client:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            return quote("Butter 500g", 5.0)

    monkeypatch.setattr(price_coordinator, "create_client", lambda *_a, **_k: Client())
    entry = fake_entry()
    entry.data = {"price_chain": "woolworths"}
    loop = asyncio.new_event_loop()
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(loop=loop, is_stopping=False),
        entry,
        summaries=lambda: ["Butter"],
    )
    coordinator.async_set_update_callback(lambda: notified.append(True))

    coordinator.async_setup()
    assert coordinator._unsub_listener is not None
    coordinator.async_update_listeners()

    assert notified == [True]


def test_pin_dispatches_exactly_once() -> None:
    """The refresh already notifies; an extra dispatch writes state twice."""

    dispatched: list[bool] = []

    class Client:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            return quote("Kids Multivitamin Gummies", 19.99)

    entry = fake_entry()
    entry.data = {"price_chain": "woolworths"}
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(loop=asyncio.new_event_loop(), is_stopping=False),
        entry,
        summaries=lambda: ["Kids vitamins"],
    )
    coordinator._client = Client()
    coordinator.async_set_update_callback(lambda: dispatched.append(True))
    coordinator._unsub_listener = coordinator.async_add_listener(
        coordinator._async_handle_update_done
    )

    asyncio.run(
        coordinator.async_pin("Kids vitamins", query="Kids Multivitamin Gummies")
    )
    assert len(dispatched) == 1

    dispatched.clear()
    asyncio.run(coordinator.async_unpin("Kids vitamins"))
    assert len(dispatched) == 1


def test_pin_notifies_immediately_when_a_refresh_is_in_flight() -> None:
    """Otherwise a cleared quote stays on screen until the in-flight request ends."""

    dispatched: list[bool] = []
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowClient:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            started.set()
            await release.wait()
            return quote("Milk 2L", 4.0)

    entry = fake_entry()
    entry.data = {"price_chain": "woolworths"}
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(loop=asyncio.new_event_loop(), is_stopping=False),
        entry,
        summaries=lambda: ["Milk"],
    )
    coordinator._client = SlowClient()
    coordinator.async_set_update_callback(lambda: dispatched.append(True))
    coordinator._unsub_listener = coordinator.async_add_listener(
        coordinator._async_handle_update_done
    )

    async def scenario() -> None:
        inflight = asyncio.create_task(coordinator.async_refresh())
        await started.wait()
        pin = asyncio.create_task(coordinator.async_pin("Milk", query="Milk 2L"))
        await asyncio.sleep(0)
        # The pin could not refresh, so it must have notified on its own.
        assert dispatched, "pin did not notify while a refresh was in flight"
        release.set()
        await asyncio.gather(inflight, pin)

    asyncio.run(scenario())


# --- price series (the only reference available for Foodstuffs) -------------


def test_series_records_a_point_per_change(monkeypatch) -> None:
    prices = {"Butter": 9.0}

    class Client:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            return quote("Pams Butter 500g", prices["Butter"])

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Butter"]
    )
    coordinator._client = Client()
    asyncio.run(coordinator._async_update_data())

    prices["Butter"] = 8.0
    coordinator._force_full = True
    asyncio.run(coordinator._async_update_data())
    prices["Butter"] = 7.0
    coordinator._force_full = True
    asyncio.run(coordinator._async_update_data())

    series = coordinator.snapshot.points_for("butter")
    assert [point.price for point in series] == [9.0, 8.0, 7.0]
    assert coordinator.snapshot.lowest_for("butter") == 7.0
    assert coordinator.snapshot.highest_for("butter") == 9.0
    assert coordinator.snapshot.is_at_lowest("butter") is True


def test_unchanged_price_does_not_grow_the_series(monkeypatch) -> None:
    """Only changes (or a 6-hour gap) are recorded, so the series stays compact."""

    class Client:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            return quote("Pams Butter 500g", 9.0)

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Butter"]
    )
    coordinator._client = Client()

    for _ in range(5):
        coordinator._force_full = True
        asyncio.run(coordinator._async_update_data())

    assert len(coordinator.snapshot.points_for("butter")) == 1


def test_series_points_are_capped_per_item(monkeypatch) -> None:
    from smart_shopping_route.const import PRICE_HISTORY_MAX_POINTS

    prices = {"n": 0}

    class Client:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            prices["n"] += 1
            return quote("Pams Butter 500g", float(prices["n"]))

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Butter"]
    )
    coordinator._client = Client()
    for _ in range(PRICE_HISTORY_MAX_POINTS + 10):
        coordinator._force_full = True
        asyncio.run(coordinator._async_update_data())

    assert len(coordinator.snapshot.points_for("butter")) == PRICE_HISTORY_MAX_POINTS


def test_series_survives_a_restart(monkeypatch) -> None:
    """A restart must not reset the reference price, or drops stop working."""

    class Client:
        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            return quote("Pams Butter 500g", 7.0)

    entry = fake_entry()
    entry.data = {"price_chain": "paknsave"}
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), entry, summaries=lambda: ["Butter"]
    )
    coordinator._price_store.initial = {
        "scope": coordinator._scope(),
        "history": {
            "butter": {
                "identity": coordinator._identity(
                    "butter", quote("Pams Butter 500g", 9.0)
                ),
                "points": [[time.time() - 3600, 9.0]],
            }
        },
    }
    asyncio.run(coordinator.async_load())
    coordinator._client = Client()

    asyncio.run(coordinator._async_update_data())

    assert coordinator.snapshot.reference_price("butter") == 9.0
    assert coordinator.snapshot.drop_for("butter") == 2.0


def test_restored_series_tolerates_malformed_entries() -> None:
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Butter"]
    )
    coordinator._price_store.initial = {
        "history": {
            "good": {"points": [[1.0, 5.0]]},
            "bad": "not a dict",
            "mixed": {"points": [[1.0, 5.0], ["nope"], None, [2.0, 6.0]]},
        }
    }

    asyncio.run(coordinator.async_load())

    assert coordinator.snapshot.history == {}
    assert [p.price for p in coordinator._history_archive["legacy:good"]] == [5.0]
    assert "legacy:bad" not in coordinator._history_archive
    assert [p.price for p in coordinator._history_archive["legacy:mixed"]] == [5.0, 6.0]


# --- Foodstuffs promotions --------------------------------------------------


def test_paknsave_marks_promotions_without_claiming_a_discount() -> None:
    """Foodstuffs flags a promotion but publishes no was-price.

    Most of those promotions are price locks whose reward value equals the shelf
    price, so the flag is context, never savings.
    """

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
                    "promotions": [
                        {"promoId": "1", "rewardValue": 739, "decal": "6000"}
                    ],
                },
                {
                    "productId": "p2",
                    "brand": "Pams",
                    "name": "Plain Butter 500g",
                    "singlePrice": {"price": 829},
                    "promotions": [],
                },
            ]
        },
    )
    client = PakNSavePriceClient(SimpleNamespace(), "store-1")
    client._session = FakeSession([token, search])

    promoted = asyncio.run(client.async_lookup("butter"))

    assert promoted is not None
    assert promoted.on_promotion is True
    # A promotion record must never be turned into a fabricated was-price.
    assert promoted.was_price is None
    assert promoted.is_special is False
    assert promoted.savings == 0.0


def test_woolworths_quotes_are_not_flagged_as_promotions() -> None:
    body = woolworths_body([
        {
            "sku": "1",
            "productName": "Anchor Butter 500g",
            "variants": [{"variantPrice": {"sellingPrice": 8.5}}],
        }
    ])
    client = WoolworthsPriceClient(SimpleNamespace())
    client._session = FakeSession([FakeResponse(200, body)])

    result = asyncio.run(client.async_lookup("butter"))

    assert result is not None
    assert result.on_promotion is False


def test_malformed_point_values_do_not_crash_restore() -> None:
    """Persisted history is read during setup; one bad value must not stop it."""

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Butter"]
    )
    coordinator._price_store.initial = {
        "history": {
            "butter": {
                "points": [
                    [1.0, 5.0],
                    [1.0, "bad"],
                    [None, 4.0],
                    ["x", "y"],
                    [2.0, 6.0],
                    [3.0, 0],
                    [4.0, -1],
                    [float("inf"), 5.0],
                    [5.0, 7.0],
                ]
            }
        }
    }

    asyncio.run(coordinator.async_load())

    assert coordinator.snapshot.history == {}
    assert [p.price for p in coordinator._history_archive["legacy:butter"]] == [
        5.0,
        6.0,
        7.0,
    ]


def test_restore_enforces_the_global_history_bound() -> None:
    """A store written before a lower bound must be trimmed on load."""

    from smart_shopping_route.const import PRICE_HISTORY_MAX

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Butter"]
    )
    coordinator._price_store.initial = {
        "history": {
            f"item {i}": {"points": [[1.0, 5.0]]} for i in range(PRICE_HISTORY_MAX + 75)
        }
    }

    asyncio.run(coordinator.async_load())

    assert len(coordinator.snapshot.history) <= PRICE_HISTORY_MAX


def test_a_drop_survives_an_incremental_refresh_that_skips_the_item(
    monkeypatch,
) -> None:
    """Adding an unrelated item must not erase a drop found earlier."""

    prices = {"Butter": 9.0}
    summaries = ["Butter"]

    class Client:
        async def async_lookup(self, summary, _pin=None, **_kwargs):
            if summary == "Butter":
                return quote("Pams Butter 500g", prices["Butter"])
            return quote("Milk 2L", 4.0)

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: list(summaries)
    )
    coordinator._client = Client()
    asyncio.run(coordinator._async_update_data())

    prices["Butter"] = 7.5
    coordinator._force_full = True
    asyncio.run(coordinator._async_update_data())
    assert coordinator.snapshot.drop_for("butter") == 1.5

    # A new item arrives; Butter is already priced so it is not re-fetched.
    summaries.append("Milk")
    coordinator._force_full = False
    asyncio.run(coordinator._async_update_data())

    assert coordinator.snapshot.drop_for("butter") == 1.5


def test_a_failed_refresh_does_not_erase_a_known_drop(monkeypatch) -> None:
    from homeassistant.helpers.update_coordinator import UpdateFailed

    class Flaky:
        def __init__(self) -> None:
            self.fail = False

        async def async_lookup(self, _summary, _pin=None, **_kwargs):
            if self.fail:
                raise PriceError("down")
            return quote("Pams Butter 500g", 7.5)

    client = Flaky()
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), fake_entry(), summaries=lambda: ["Butter"]
    )
    coordinator._client = client
    coordinator._identities["butter"] = coordinator._identity(
        "butter", quote("Pams Butter 500g", 9.0)
    )
    coordinator._snapshot.history["butter"] = [
        price_coordinator.PricePoint(time.time() - 3600, 9.0)
    ]
    asyncio.run(coordinator._async_update_data())
    assert coordinator.snapshot.drop_for("butter") == 1.5

    client.fail = True
    coordinator._force_full = True
    with pytest.raises(UpdateFailed):
        asyncio.run(coordinator._async_update_data())

    assert coordinator.snapshot.drop_for("butter") == 1.5


# --- size and unit price ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "label", "amount", "unit"),
    [
        ("Weet-Bix Cereal 1.2kg", "1.2kg", 1.2, "kg"),
        ("Anchor Butter 500g", "500g", 0.5, "kg"),
        ("Musashi Energy Drink 500ml", "500ml", 0.5, "L"),
        ("Pams Milk 2L", "2L", 2.0, "L"),
        ("Toilet Paper 12pk", "12pk", 12.0, "ea"),
        ("Juice 6 x 250ml", "6 x 250ml", 1.5, "L"),
        ("Juice 250ml x 6", "6 x 250ml", 1.5, "L"),
        ("Vitamins 110 Pack", "110Pack", 110.0, "ea"),
        ("Something Without A Size", None, None, None),
    ],
)
def test_size_is_parsed_from_the_product_name(text, label, amount, unit) -> None:
    from smart_shopping_route.prices import size_from_text

    parsed = size_from_text(text)

    if label is None:
        assert parsed is None
    else:
        assert parsed == (label, amount, unit)


@pytest.mark.parametrize(
    ("price", "text", "expected"),
    [
        (8.96, "Weet-Bix 1.2kg", "$7.47/kg"),
        (4.00, "Pams Milk 2L", "$2.00/L"),
        (4.55, "Example Milk 2L", "$2.28/L"),
        (7.39, "Pure Butter 500g", "$14.78/kg"),
        (6.59, "Toilet Paper 12pk", "$0.55/ea"),
        (9.0, "Juice 6 \u00d7 250ml", "$6.00/L"),
        (22.0, "Gummies 110 Pack", "$0.20/ea"),
        (5.00, "No Size Here", None),
    ],
)
def test_unit_price_is_computed(price, text, expected) -> None:
    from smart_shopping_route.prices import compute_unit_price, size_from_text

    assert compute_unit_price(price, size_from_text(text)) == expected


def test_retailer_comparative_price_is_preferred() -> None:
    """Foodstuffs quotes its own per-100g figure, which is what shoppers see."""

    from smart_shopping_route.prices import _comparative_unit_price

    assert (
        _comparative_unit_price({
            "pricePerUnit": 148,
            "unitQuantity": 100,
            "unitQuantityUom": "g",
        })
        == "$14.80/kg"
    )
    assert (
        _comparative_unit_price({
            "pricePerUnit": 31,
            "unitQuantity": 100,
            "unitQuantityUom": "sheets",
        })
        == "$0.31/100 sheets"
    )
    assert _comparative_unit_price({}) is None


def test_woolworths_quote_carries_size_and_unit_price() -> None:
    body = woolworths_body([
        {
            "sku": "1",
            "productName": "Weet-Bix Cereal 1.2kg",
            "variants": [{"variantPrice": {"sellingPrice": 8.96}}],
        }
    ])
    client = WoolworthsPriceClient(SimpleNamespace())
    client._session = FakeSession([FakeResponse(200, body)])

    result = asyncio.run(client.async_lookup("weet-bix"))

    assert result is not None
    assert result.size == "1.2kg"
    assert result.unit_price == "$7.47/kg"


def test_woolworths_weight_only_variant_uses_the_formatted_kilo_price() -> None:
    body = woolworths_body([
        {
            "sku": "1",
            "productName": "Loose Bananas",
            "variants": [
                {"unitOfMeasure": "KG", "variantPrice": {"sellingPrice": 2.675}}
            ],
        }
    ])
    client = WoolworthsPriceClient(SimpleNamespace())
    client._session = FakeSession([FakeResponse(200, body)])

    result = asyncio.run(client.async_lookup("bananas"))

    assert result is not None
    assert result.size is None
    assert result.unit_price == "$2.68/kg"


def test_paknsave_quote_uses_the_comparative_price() -> None:
    token = FakeResponse(200, {"access_token": "anon"})
    search = FakeResponse(
        200,
        {
            "products": [
                {
                    "productId": "p1",
                    "brand": "Pams",
                    "name": "Pure Butter 500g",
                    "singlePrice": {
                        "price": 739,
                        "comparativePrice": {
                            "pricePerUnit": 148,
                            "unitQuantity": 100,
                            "unitQuantityUom": "g",
                        },
                    },
                }
            ]
        },
    )
    client = PakNSavePriceClient(SimpleNamespace(), "store-1")
    client._session = FakeSession([token, search])

    result = asyncio.run(client.async_lookup("butter"))

    assert result is not None
    assert result.size == "500g"
    assert result.unit_price == "$14.78/kg"


@pytest.mark.parametrize(
    ("pack", "expected_size", "expected_unit"),
    [
        ("500g", "500g", "$14.78/kg"),
        ("2.25kg", "2.25kg", "$3.28/kg"),
        ("", None, "$14.80/kg"),
    ],
)
def test_exact_pack_metadata_not_reverse_rounded_price(
    pack, expected_size, expected_unit
):
    client = PakNSavePriceClient(SimpleNamespace(), "store-1")
    client._session = FakeSession([
        FakeResponse(200, {"access_token": "anon"}),
        FakeResponse(
            200,
            {
                "products": [
                    {
                        "productId": "p1",
                        "name": "Pure Butter",
                        "displayName": pack,
                        "singlePrice": {
                            "price": 739,
                            "comparativePrice": {
                                "pricePerUnit": 148,
                                "unitQuantity": 100,
                                "unitQuantityUom": "g",
                            },
                        },
                    }
                ]
            },
        ),
    ])
    result = asyncio.run(client.async_lookup("butter"))
    assert result.size == expected_size
    assert result.unit_price == expected_unit


@pytest.mark.parametrize(
    ("price", "pack", "expected"),
    [
        (8.29, "8 x 270 sheets", "$0.38/100 sheets"),
        (0.40, "200 sheet", "$0.20/100 sheets"),
        (1.0, "1000 count", "<$0.01/ea"),
        (1.0, "200 count", "$0.01/ea"),
    ],
)
def test_subcent_unit_prices_do_not_look_free(price, pack, expected):
    from smart_shopping_route.prices import compute_unit_price, size_from_text

    assert compute_unit_price(price, size_from_text(pack)) == expected


@pytest.mark.parametrize(
    ("cents", "amount", "unit", "expected"),
    [
        (38, 100, "sheets", "$0.38/100 sheets"),
        (0.38, 1, "sheet", "$0.38/100 sheets"),
        (0.1, 1, "ea", "<$0.01/ea"),
        (0.5, 1, "ea", "$0.01/ea"),
        (1, 1e-321, "g", None),
    ],
)
def test_comparative_subcent_prices_are_readable(cents, amount, unit, expected):
    from smart_shopping_route.prices import _comparative_unit_price

    assert (
        _comparative_unit_price({
            "pricePerUnit": cents,
            "unitQuantity": amount,
            "unitQuantityUom": unit,
        })
        == expected
    )


def test_comparative_unit_price_drops_a_redundant_one() -> None:
    from smart_shopping_route.prices import _comparative_unit_price

    assert (
        _comparative_unit_price({
            "pricePerUnit": 698,
            "unitQuantity": 1,
            "unitQuantityUom": "L",
        })
        == "$6.98/L"
    )


def test_a_qualifier_variant_below_the_first_eight_hits_is_still_found() -> None:
    """Regression: the olive-oil tuna was silently unpriced.

    The plain term "Tuna 185g" returns relevance-ordered hits, and the
    olive-oil variant ranks 12th. A candidate pool of 8 cut it off, so the item
    matched nothing even though the retailer stocks it.
    """

    from smart_shopping_route.prices import (
        SEARCH_PAGE_SIZE,
        PakNSavePriceClient,
    )

    hits = [
        "Sealord Smoked Flavour Tuna",
        "Sealord Chunky Style Tuna In Spring Water",
        "Pams Tuna Chunks In Soya Oil",
        "Sealord Sweet Thai Chilli Tuna",
        "Pams Lemon Pepper Tuna",
        "Pams Tuna Chunks In Spring Water",
        "Pams Sweet Thai Chilli Tuna",
        "Sealord Lemon Pepper Flavour Tuna",
        "Sealord Tuna In Spring Water",
        "Pams Tuna In Spring Water",
        "John West Tuna In Spring Water",
        "Sealord Olive Oil Blend Tuna",
    ]
    body = {
        "products": [
            {
                "productId": f"p{index}",
                "brand": name.split(" ", 1)[0],
                "name": name.split(" ", 1)[1],
                "displayName": "185g",
                "singlePrice": {"price": 349},
            }
            for index, name in enumerate(hits)
        ]
    }
    client = PakNSavePriceClient(SimpleNamespace(), "store-1")
    client._session = FakeSession([
        FakeResponse(200, {"access_token": "anon"}),
        FakeResponse(200, body),
    ])

    result = asyncio.run(client.async_lookup("Tuna 185g (olive oil)"))

    assert SEARCH_PAGE_SIZE >= len(hits)
    assert result is not None
    assert result.product_name == "Sealord Olive Oil Blend Tuna"
    assert result.price == 3.49
    search = next(c for c in client._session.calls if "products" in c["url"])
    assert search["json"]["hitsPerPage"] == SEARCH_PAGE_SIZE


def test_a_missed_cycle_does_not_make_the_basket_stale(monkeypatch) -> None:
    """Staleness must outlive the interval, or one hiccup relabels every row."""

    coordinator, _calls = make_price_coordinator(monkeypatch, ["Milk"], {})
    snapshot = coordinator.snapshot
    snapshot.quotes["milk"] = quote("Milk 2L", 4.0)
    snapshot.active = {"milk": 1}

    # Longer than one interval, still well inside the display threshold.
    snapshot.observed_at["milk"] = time.time() - (PRICE_REFRESH_HOURS * 3600 + 60)
    assert PRICE_STALE_AFTER_HOURS > PRICE_REFRESH_HOURS
    assert not snapshot.stale_for("milk")
    assert snapshot.stale_count == 0

    snapshot.observed_at["milk"] = time.time() - (PRICE_STALE_AFTER_HOURS * 3600 + 60)
    assert snapshot.stale_for("milk")
    assert snapshot.stale_count == 1


def test_a_readded_item_imports_its_store_history_again(monkeypatch) -> None:
    """Removing an item discards its imported history, so the cache must go too.

    Otherwise the "already backfilled" flag outlived the data it described and a
    re-added Woolworths item showed no in-store history until the next mirror
    sync or restart.
    """

    from smart_shopping_route.grocer import GrocerMatch, GrocerPrice, GrocerProduct

    summaries = ["Milk"]
    store_quote = PriceQuote(
        chain=CHAIN_WOOLWORTHS, product_id="p", product_name="Milk 2L", price=4.0
    )
    coordinator, _calls = make_price_coordinator(
        monkeypatch, summaries, {"Milk": store_quote}
    )
    coordinator.chain = CHAIN_WOOLWORTHS
    coordinator.grocer_store_id = 228
    match = GrocerMatch(
        product=GrocerProduct(6403, "Milk", "Example", "2L"),
        price=GrocerPrice(6403, 4.0, None),
        history=[(1000.0, 3.0), (2000.0, 4.0)],
    )

    async def fake_match(*_args, **_kwargs):
        return match

    coordinator._grocer_data = SimpleNamespace(async_match_with_history=fake_match)

    asyncio.run(coordinator._async_update_data())
    assert coordinator.snapshot.store_history["milk"]

    summaries.clear()
    asyncio.run(coordinator._async_update_data())
    assert "milk" not in coordinator.snapshot.store_history

    summaries.append("Milk")
    asyncio.run(coordinator._async_update_data())

    assert coordinator.snapshot.store_history["milk"], (
        "a re-added item must import its store history again"
    )


def test_an_unavailable_source_does_not_clear_the_price_cache(monkeypatch) -> None:
    """A restart or refresh during an outage must keep the prices we collected.

    The source reports no items while it is unavailable, and pruning against
    that reading used to drop every quote, store history and product match.
    """

    summaries = ["Milk"]
    available = {"value": True}
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(),
        fake_entry(),
        summaries=lambda: list(summaries),
        authoritative=lambda: available["value"],
    )

    class Client:
        async def async_lookup(self, summary, pin=None, **_kwargs):
            return quote("Milk", 4.0) if summary == "Milk" else None

    coordinator._client = Client()

    first = asyncio.run(coordinator._async_update_data())
    assert set(first.quotes) == {"milk"}

    available["value"] = False
    summaries.clear()
    during = asyncio.run(coordinator._async_update_data())

    assert set(during.quotes) == {"milk"}
    assert during.active == {"milk": 1}
    assert during.priced_count == 1

    # Recovery prunes normally: the reading is authoritative again.
    available["value"] = True
    recovered = asyncio.run(coordinator._async_update_data())

    assert recovered.quotes == {}
    assert recovered.active == {}


# --- a ruled-out variant ----------------------------------------------------


def centrum(*names: str) -> list[PriceQuote]:
    return [
        PriceQuote(
            chain=CHAIN_PAKNSAVE,
            product_id=name,
            product_name=name,
            price=24.99,
            size="100pk",
        )
        for name in names
    ]


def test_a_negated_quantity_disqualifies_that_variant() -> None:
    """Writing "not 50+" used to vote *for* the 50+ product.

    The word was counted as content and "50" matched the candidate, so the wrong
    formulation scored higher than the right one and won.
    """

    item = "Centrum advance Adult 100pk (not 50+)"
    candidates = centrum(
        "Centrum Advance 50+ Adults Film Coated Tablets",
        "Centrum Advance Adult 100 Tablets",
    )

    best = best_match(item, candidates)

    assert best is not None
    assert best.product_name == "Centrum Advance Adult 100 Tablets"


def test_a_plus_qualifier_is_not_the_bare_number() -> None:
    """ "50+" is an age formulation; "50" is a quantity. They must differ."""

    assert prices_tokens("50+") == ["50+"]
    assert prices_tokens("50") == ["50"]
    assert excluded_tokens("Vitamins (not 50+)") == {"50+"}


def test_no_added_sugar_is_not_treated_as_an_exclusion() -> None:
    """A negated phrase with no quantity describes the product, not a rejection.

    Retailer titles state these positively, so excluding the words would discard
    the very product the household wants.
    """

    item = "Baked beans no added sugar"

    assert excluded_tokens(item) == set()
    best = best_match(
        item,
        [
            PriceQuote(
                chain=CHAIN_PAKNSAVE,
                product_id="orig",
                product_name="Wattie's Baked Beans Original 420g",
                price=2.5,
                size="420g",
            ),
            PriceQuote(
                chain=CHAIN_PAKNSAVE,
                product_id="nas",
                product_name="Wattie's Baked Beans No Added Sugar 420g",
                price=2.5,
                size="420g",
            ),
        ],
    )
    assert best is not None
    assert "No Added Sugar" in best.product_name


def test_pack_size_does_not_dilute_the_match_score() -> None:
    """The pack size is enforced exactly, so counting it again only hurt.

    "100pk" is printed on the pack, not in the product title, so the correct
    product lost a fifth of its score for a constraint already satisfied.
    """

    with_size = match_score("Centrum Adult 100pk", "Centrum Advance Adult")
    without_size = match_score("Centrum Adult", "Centrum Advance Adult")

    assert with_size == without_size


def test_shortlist_offers_only_candidates_that_passed_the_hard_constraints() -> None:
    """A tie-break may choose among real options, never invent one."""

    item = "Centrum advance Adult 100pk (not 50+)"
    candidates = centrum(
        "Centrum Advance 50+ Adults Film Coated Tablets",
        "Centrum Advance Adult 100 Tablets",
        "Centrum Advance Adult 50 Tablets",
    )

    offered = [candidate.product_name for candidate in shortlist(item, candidates)]

    assert offered == ["Centrum Advance Adult 100 Tablets"]


# --- the AI tie-break hook --------------------------------------------------


class ScriptedClient(WoolworthsPriceClient):
    """A real client with the network replaced by a scripted candidate list."""

    def __init__(self, candidates: list[PriceQuote]) -> None:
        self.candidates = candidates
        self.terms: list[str] = []

    async def async_candidates(self, term: str) -> list[PriceQuote]:
        self.terms.append(term)
        return list(self.candidates)


def test_a_ranker_is_only_asked_when_scoring_finds_nothing() -> None:
    """A confident deterministic match must never cost a provider request."""

    asked: list[int] = []

    async def ranker(_summary, candidates):
        asked.append(len(candidates))
        return candidates[0]

    client = ScriptedClient(centrum("Centrum Advance Adult 100 Tablets"))

    asyncio.run(
        client.async_lookup(
            "Centrum advance Adult 100pk (not 50+)", None, ranker=ranker
        )
    )

    assert asked == []


def test_a_ranker_resolves_an_item_no_title_shares_words_with() -> None:
    """The long tail this exists for: the shop names it differently.

    "Nappies" and "Nappy Pants" are the same thing to a person and share no
    token that scoring accepts, so the item stays unpriced without a tie-break.
    """

    item = "Nappies"
    candidates = [
        PriceQuote(
            chain=CHAIN_WOOLWORTHS,
            product_id="huggies",
            product_name="Huggies Ultra Dry Nappy Pants Walker Boy 32pk",
            price=24.0,
        ),
        PriceQuote(
            chain=CHAIN_WOOLWORTHS,
            product_id="wipes",
            product_name="Huggies Baby Wipes Fragrance Free 80pk",
            price=6.0,
        ),
    ]
    asked: list[list[str]] = []

    async def ranker(summary, offered):
        asked.append([candidate.product_name for candidate in offered])
        assert summary == item
        return offered[0]

    picked = asyncio.run(
        ScriptedClient(candidates).async_lookup(item, None, ranker=ranker)
    )

    assert asked == [
        [
            "Huggies Ultra Dry Nappy Pants Walker Boy 32pk",
            "Huggies Baby Wipes Fragrance Free 80pk",
        ]
    ]
    assert picked is not None
    assert picked.product_id == "huggies"


def test_a_ranker_is_never_offered_a_ruled_out_variant() -> None:
    """The constraint is enforced before the model sees anything."""

    seen: list[list[str]] = []

    async def ranker(_summary, offered):
        seen.append([candidate.product_name for candidate in offered])
        return offered[0] if offered else None

    client = ScriptedClient(centrum("Centrum Advance 50+ Adults Film Coated Tablets"))

    picked = asyncio.run(
        client.async_lookup(
            "Centrum advance Adult 100pk (not 50+)", None, ranker=ranker
        )
    )

    # The only candidate was the formulation the item rules out, so the tie-break
    # was offered nothing at all and the item stays honestly unpriced.
    assert seen == [[]]
    assert picked is None


# --- remembering an AI decision ---------------------------------------------


def ranker_entry(**data):
    """A profile that opted into the AI tie-break."""

    entry = fake_entry()
    entry.data = {
        "price_chain": "woolworths",
        "ai_task_entity_id": "ai_task.synthetic",
        "ai_match": True,
        **data,
    }
    return entry


PANTS = "Huggies Ultra Dry Nappy Pants Walker Boy 32pk"


class RankingClient:
    """Scripted client that records how often the tie-break was consulted."""

    def __init__(self, quotes: dict[str, PriceQuote], stock: bool = True) -> None:
        self.quotes = quotes
        self.stock = stock
        self.ranked: list[str] = []

    async def async_lookup(self, summary, pin=None, *, ranker=None, **_kwargs):
        if pin is not None and pin.product_id:
            # An exact pin resolves only while that product is still listed.
            if not self.stock:
                return None
            return PriceQuote(
                chain=CHAIN_WOOLWORTHS,
                product_id=pin.product_id,
                product_name=pin.product_name or PANTS,
                price=24.0,
            )
        if summary in self.quotes:
            return self.quotes[summary]
        if ranker is None or not self.stock:
            return None
        self.ranked.append(summary)
        offered = [
            PriceQuote(
                chain=CHAIN_WOOLWORTHS,
                product_id="pants",
                product_name=PANTS,
                price=24.0,
            )
        ]
        return await ranker(summary, offered)


def make_ranking_coordinator(monkeypatch, quotes, summaries=("Nappies",)):
    async def generate(*_args, **_kwargs):
        return SimpleNamespace(data={"index": 0})

    monkeypatch.setattr(prices_ai_match, "async_generate_data", generate)
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(loop=asyncio.new_event_loop(), is_stopping=False),
        ranker_entry(),
        summaries=lambda: list(summaries),
    )
    coordinator._client = RankingClient(quotes)
    coordinator._ranker = coordinator._build_ranker()
    return coordinator


def test_an_ai_decision_is_remembered_so_it_cannot_flip(monkeypatch) -> None:
    """Re-asking every cycle would rewrite the price series each refresh.

    The answer is cached as an automatic pin, so the second refresh makes no
    provider request at all.
    """

    coordinator = make_ranking_coordinator(monkeypatch, {})

    asyncio.run(coordinator._async_update_data())
    first = coordinator.quote_for("Nappies")
    asyncio.run(coordinator._async_update_data())

    assert first is not None
    assert coordinator._client.ranked == ["Nappies"]
    pin = coordinator.pin_for("Nappies")
    assert pin is not None
    assert pin.is_auto
    assert pin.product_id == "pants"


def test_the_tie_break_is_off_unless_the_profile_asks_for_it() -> None:
    """Opt-in: item names must not reach a provider by default."""

    entry = fake_entry()
    entry.data = {"price_chain": "woolworths", "ai_task_entity_id": "ai_task.x"}
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(loop=asyncio.new_event_loop(), is_stopping=False),
        entry,
        summaries=lambda: ["Nappies"],
    )
    coordinator._client = RankingClient({})

    asyncio.run(coordinator._async_update_data())

    assert coordinator.ai_match_enabled is False
    assert coordinator._client.ranked == []


def test_an_automatic_pin_that_stops_resolving_is_dropped(monkeypatch) -> None:
    """A cached answer is not a decision: when the shop changes, match afresh."""

    coordinator = make_ranking_coordinator(monkeypatch, {})
    asyncio.run(coordinator._async_update_data())
    assert coordinator.pin_for("Nappies") is not None

    # The product disappears from the shop, and nothing else is offered. A
    # manual refresh is the household asking now, which is when a delisted
    # product is noticed.
    coordinator._client.stock = False
    coordinator._pins["nappies"] = coordinator._pins["nappies"].__class__(
        chain="woolworths",
        query="Nappies",
        product_id="gone",
        product_name="Delisted Product",
        source="auto",
    )
    coordinator._force_full = True

    asyncio.run(coordinator._async_update_data())

    assert coordinator.pin_for("Nappies") is None
    assert coordinator.quote_for("Nappies") is None


def test_a_user_pin_is_never_replaced_by_the_tie_break(monkeypatch) -> None:
    """The household's own choice outranks any automatic answer."""

    coordinator = make_ranking_coordinator(monkeypatch, {})
    asyncio.run(
        coordinator.async_pin(
            "Nappies", query="Huggies nappy pants", product_id="chosen"
        )
    )
    before = coordinator.pin_for("Nappies")

    asyncio.run(coordinator._async_update_data())

    after = coordinator.pin_for("Nappies")
    assert after is not None
    assert after.product_id == before.product_id
    assert after.source == "user"
    assert coordinator._client.ranked == []


def test_an_unstated_pack_size_is_not_a_contradiction() -> None:
    """Pack counts live on the box, not in the title.

    The plain Centrum Advance Adult product is listed as "Centrum Advance
    Adults Film Coated Tablets" with no pack count at all, so rejecting it for
    silence left the item unpriced when it was the only correct candidate.
    """

    item = "Centrum advance Adult 100pk (not 50+)"
    best = best_match(
        item,
        [
            PriceQuote(
                chain=CHAIN_PAKNSAVE,
                product_id="50plus",
                product_name="Centrum Advance 50+ Adults Film Coated Tablets",
                price=24.99,
            ),
            PriceQuote(
                chain=CHAIN_PAKNSAVE,
                product_id="plain",
                product_name="Centrum Advance Adults Film Coated Tablets",
                price=18.99,
            ),
        ],
    )

    assert best is not None
    assert best.product_id == "plain"
    assert best.price == 18.99


def test_a_stated_pack_size_that_differs_is_still_rejected() -> None:
    """Silence is tolerated; a contradiction is not."""

    item = "Centrum advance Adult 100pk (not 50+)"
    best = best_match(
        item,
        [
            PriceQuote(
                chain=CHAIN_PAKNSAVE,
                product_id="thirty",
                product_name="Centrum Advance Adults Film Coated Tablets",
                price=12.0,
                size="30pk",
            ),
        ],
    )

    assert best is None


def test_a_query_too_specific_falls_back_to_a_looser_one() -> None:
    """The pack count is on the box, so searching it hides the right product.

    Searching "Centrum advance Adult 100pk" returns only the 50+ box; the plain
    product is listed without a pack count at all. The retry therefore drops the
    size from the query, while the size is still enforced on the results.
    """

    item = "Centrum advance Adult 100pk (not 50+)"
    plain = PriceQuote(
        chain=CHAIN_PAKNSAVE,
        product_id="plain",
        product_name="Centrum Advance Adults Film Coated Tablets",
        price=18.99,
    )
    fifty = PriceQuote(
        chain=CHAIN_PAKNSAVE,
        product_id="50plus",
        product_name="Centrum Advance 50+ Adults Film Coated Tablets",
        price=24.99,
    )
    results = {
        "Centrum advance Adult 100pk": [fifty],
        "Centrum advance Adult 100pk not 50+": [fifty, plain],
    }

    class Scripted(PriceClient):
        def __init__(self) -> None:
            self.terms: list[str] = []

        async def async_candidates(self, term: str) -> list[PriceQuote]:
            self.terms.append(term)
            return list(results.get(term, []))

    client = Scripted()
    best = asyncio.run(client.async_lookup(item))

    assert best is not None
    assert best.product_id == "plain"
    assert client.terms[0] == "Centrum advance Adult 100pk"
    assert "Centrum advance Adult 100pk not 50+" in client.terms


def test_a_size_free_retry_is_offered_when_the_qualifier_retry_is_the_same() -> None:
    """An item with no parenthetical still gets a looser second attempt."""

    assert size_free_term("Centrum advance Adult 100pk") == "Centrum advance Adult"
    assert size_free_term("Centrum advance Adult 100pk (not 50+)") == (
        "Centrum advance Adult"
    )
    # No size to drop, so no retry is invented.
    assert size_free_term("Toilet paper") == ""


# --- value ranking ----------------------------------------------------------


def priced(name: str, price: float, size: str | None = None) -> PriceQuote:
    return PriceQuote(
        chain=CHAIN_PAKNSAVE,
        product_id=name,
        product_name=name,
        price=price,
        size=size,
    )


def test_alternatives_are_cheapest_per_unit_first() -> None:
    """The list answers "is the same thing cheaper elsewhere".

    Only the pack the item asks for is offered, so every option is the same
    thing and the cheapest per unit is simply the cheapest to buy.
    """

    item = "Centrum advance Adult 100pk (not 50+)"
    options = alternatives(
        item,
        [
            priced("Centrum Advance Adult 100 Tablets", 24.99, "100pk"),
            priced("Centrum Advance Adults 100 Tablets", 19.99, "100pk"),
            priced("Centrum Advance Adult Multivitamin 100 Tablets", 29.99, "100pk"),
        ],
    )

    assert [option.product_id for option in options] == [
        "Centrum Advance Adults 100 Tablets",
        "Centrum Advance Adult 100 Tablets",
        "Centrum Advance Adult Multivitamin 100 Tablets",
    ]


def test_alternatives_exclude_a_different_pack_size() -> None:
    """A bigger box is a different purchase, not a cheaper version of this one."""

    options = alternatives(
        "Centrum advance Adult 100pk (not 50+)",
        [
            priced("Centrum Advance Adults 200 Tablets", 39.99, "200pk"),
            priced("Centrum Advance Adults 60 Tablets", 19.99, "60pk"),
        ],
    )

    assert options == []


def test_alternatives_never_include_a_ruled_out_or_mispacked_product() -> None:
    item = "Centrum advance Adult 100pk (not 50+)"

    options = alternatives(
        item,
        [
            priced("Centrum Advance 50+ Adults 100 Tablets", 14.99, "100pk"),
            priced("Centrum Advance Adults 30 Tablets", 9.99, "30pk"),
            priced("Centrum Advance Adult 100 Tablets", 24.99, "100pk"),
        ],
    )

    assert [option.product_id for option in options] == [
        "Centrum Advance Adult 100 Tablets"
    ]


def test_no_alternatives_without_a_stated_pack_size() -> None:
    """A per-unit price of a different pack is not an alternative."""

    assert alternatives("Strawberries", [priced("Punnet", 7.9, "250g")]) == []


def test_alternatives_are_capped() -> None:
    item = "Milk 2L"
    options = alternatives(
        item,
        [priced(f"Brand {index} Milk 2L", 4.0 + index, "2L") for index in range(9)],
    )

    assert len(options) == ALTERNATIVE_COUNT
    assert options[0].product_id == "Brand 0 Milk 2L"


def test_a_stated_pack_size_prefers_the_better_value_of_two_equal_matches() -> None:
    """Equally relevant and the same pack means the same product, cheaper wins."""

    item = "Centrum advance Adult 100pk (not 50+)"
    best = best_match(
        item,
        [
            priced("Centrum Advance Adult 100 Tablets", 29.99, "100pk"),
            priced("Centrum Advance Adults Film Coated 100 Tablets", 24.99, "100pk"),
        ],
    )

    assert best is not None
    assert best.price == 24.99


def test_without_a_pack_size_the_retailer_order_still_decides() -> None:
    """Value must not override relevance when the pack is unstated.

    "Strawberries" scores equally for a punnet and for strawberry wafers, and
    the wafers are cheaper per gram. Buying them would be wrong, so the
    retailer's own relevance order stands.
    """

    best = best_match(
        "Strawberries",
        [
            priced("Woolworths Fresh Strawberry 250g Punnet", 7.9, "250g"),
            priced("Woolworths Creme Filled Strawberry Wafers 125g", 1.09, "125g"),
        ],
    )

    assert best is not None
    assert "Punnet" in best.product_name


def test_a_matched_item_reports_its_alternatives() -> None:
    """A priced row must carry the comparison, not just an unpriced one.

    The match is chosen for relevance, so the alternatives are the only place a
    shopper learns whether the pick was also the best value. Collecting them
    only when scoring failed left every priced row without one.
    """

    client = WoolworthsPriceClient(SimpleNamespace())
    shelf = [
        priced("Centrum Advance Adult 100 Tablets", 24.99, "100pk"),
        priced("Centrum Advance Adults 100 Tablets", 19.99, "100pk"),
        priced("Centrum Advance Adult Multivitamin 100 Tablets", 29.99, "100pk"),
    ]

    async def candidates(_term):
        return list(shelf)

    client.async_candidates = candidates  # type: ignore[method-assign]
    found: list[PriceQuote] = []

    result = asyncio.run(
        client.async_lookup(
            "Centrum advance Adult 100pk (not 50+)", alternatives_out=found
        )
    )

    assert result is not None
    assert [option.product_id for option in found] == [
        "Centrum Advance Adults 100 Tablets",
        "Centrum Advance Adult 100 Tablets",
        "Centrum Advance Adult Multivitamin 100 Tablets",
    ]


def test_a_matched_item_reports_alternatives_after_a_retry() -> None:
    """A match found by the looser query still reports what that query saw."""

    client = WoolworthsPriceClient(SimpleNamespace())
    seen: list[str] = []

    async def candidates(term):
        seen.append(term)
        if term == "Centrum advance Adult 100pk":
            return []
        return [priced("Centrum Advance Adults 100 Tablets", 19.99, "100pk")]

    client.async_candidates = candidates  # type: ignore[method-assign]
    found: list[PriceQuote] = []

    result = asyncio.run(
        client.async_lookup(
            "Centrum advance Adult 100pk (not 50+)", alternatives_out=found
        )
    )

    assert result is not None
    assert len(seen) > 1
    assert [option.product_id for option in found] == [
        "Centrum Advance Adults 100 Tablets"
    ]


def test_alternatives_are_only_products_that_are_the_same_thing() -> None:
    """A pack size is not a substitute: the qualifier has to match too.

    "Tuna 185g (olive oil)" sits beside Lemon Pepper and Sweet Thai Chilli on the
    same shelf at the same price. Offering them as cheaper alternatives would
    recommend exactly the flavours the item ruled out.
    """

    options = alternatives(
        "Tuna 185g (olive oil)",
        [
            priced("Pams Lemon Pepper Tuna", 2.39, "185g"),
            priced("Pams Sweet Thai Chilli Tuna", 2.39, "185g"),
            priced("Pams Tuna Chunks In Olive Oil Blend", 2.39, "185g"),
        ],
    )

    assert [option.product_id for option in options] == [
        "Pams Tuna Chunks In Olive Oil Blend"
    ]


def test_an_unpriced_item_gets_no_alternatives_from_another_family() -> None:
    """Nothing matched, so a 100-pack of gummies is not a substitute vitamin."""

    options = alternatives(
        "Centrum advance Adult 100pk (not 50+)",
        [
            priced(
                "Natures Way Women's Multivitamin Adult Vita Gummies", 17.49, "100pk"
            ),
            priced("Centrum Advance Adults 100 Tablets", 33.99, "100pk"),
        ],
    )

    assert [option.product_id for option in options] == [
        "Centrum Advance Adults 100 Tablets"
    ]
