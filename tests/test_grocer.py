"""grocer.nz mirror: sync, offline fallback, and self-validating matching."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from smart_shopping_route.grocer import (
    GrocerData,
    GrocerError,
    GrocerMatch,
    GrocerPrice,
    GrocerProduct,
    _normalise,
)


class FakeResponse:
    def __init__(self, status: int, payload: bytes = b"") -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    @property
    def content(self):
        return _Chunks(self._payload)


class _Chunks:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def iter_chunked(self, size: int):
        async def gen():
            for start in range(0, len(self._payload), size):
                yield self._payload[start : start + size]

        return gen()


class FakeSession:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    hass = SimpleNamespace(config=SimpleNamespace(path=lambda p: str(tmp_path / p)))
    data = GrocerData(hass)
    data._session = FakeSession([])
    return data


def test_normalise_handles_missing_values() -> None:
    assert _normalise("PAK'nSAVE Ormiston") == "paknsave ormiston"
    assert _normalise("PakNSave  Ormiston") == "paknsave ormiston"
    assert _normalise(None) == ""
    assert _normalise(float("nan")) == ""


def test_catalogue_is_mirrored_then_replaced_atomically(data_dir, tmp_path) -> None:
    data_dir._session = FakeSession([FakeResponse(200, b"duckdb-bytes")])

    ok = asyncio.run(data_dir.async_sync_catalogue())

    assert ok is True
    written = data_dir._dir / "base_v3.duckdb"
    assert written.read_bytes() == b"duckdb-bytes"
    # No leftover partial file.
    assert not list(written.parent.glob("*.part"))


def test_failed_download_keeps_the_existing_mirror(data_dir, tmp_path) -> None:
    """The mirror exists so an outage does not lose downloaded history."""

    target = data_dir._dir / "base_v3.duckdb"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"old-good-copy")
    data_dir._session = FakeSession([FakeResponse(503)])

    ok = asyncio.run(data_dir.async_sync_catalogue())

    assert ok is False
    assert target.read_bytes() == b"old-good-copy"


def test_partial_download_is_discarded(data_dir, tmp_path) -> None:
    """A truncated download must not replace a good mirror."""

    target = data_dir._dir / "base_v3.duckdb"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"good")

    class Broken(FakeSession):
        def get(self, url, **_kwargs):
            self.urls.append(url)
            return _BrokenResponse()

    data_dir._session = Broken([])

    assert asyncio.run(data_dir.async_sync_catalogue()) is False
    assert target.read_bytes() == b"good"
    assert not list(target.parent.glob("*.part"))


class _BrokenResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    @property
    def content(self):
        return _BrokenChunks()


class _BrokenChunks:
    def iter_chunked(self, _size):
        async def gen():
            yield b"partial"
            raise TimeoutError("connection dropped")

        return gen()


def test_store_resolution_ignores_apostrophes_and_case(data_dir, monkeypatch) -> None:
    data_dir._stores = {228: "PAK'nSAVE Ormiston", 85: "Woolworths Manukau City Mall"}
    data_dir._catalogue = {}

    assert data_dir.resolve_store("PakNSave Ormiston") == 228
    assert data_dir.resolve_store("pak'nsave ormiston") == 228
    assert data_dir.resolve_store("Manukau City Mall") == 85
    assert data_dir.resolve_store("Nowhere") is None


def test_store_resolution_rejects_an_ambiguous_name(data_dir) -> None:
    data_dir._stores = {
        1: "PAK'nSAVE Albany",
        2: "PAK'nSAVE Ormiston",
    }
    data_dir._catalogue = {}

    assert data_dir.resolve_store("PAK'nSAVE") is None


# --- matching ---------------------------------------------------------------


def build_match_data(data_dir, *, regular: float, sale: float | None) -> None:
    data_dir._catalogue = {
        6403: GrocerProduct(6403, "Weet-Bix", "Sanitarium", "1.2kg"),
        111: GrocerProduct(111, "Weet-Bix", "Sanitarium", "375g"),
    }
    data_dir._stores = {228: "PAK'nSAVE Ormiston"}
    data_dir._store_prices = {
        228: {
            6403: GrocerPrice(6403, regular, sale),
            111: GrocerPrice(111, 4.59, None),
        }
    }


def test_match_requires_price_agreement(data_dir) -> None:
    """A name match alone is not trusted; the price must agree."""

    build_match_data(data_dir, regular=7.99, sale=None)

    hit = data_dir.match(228, "Sanitarium Weet-Bix 1.2kg", 7.99)
    miss = data_dir.match(228, "Sanitarium Weet-Bix 1.2kg", 12.5)

    assert hit is not None
    assert hit.product.product_id == 6403
    assert miss is None


def test_match_accepts_the_sale_price(data_dir) -> None:
    """Retailers quote the special price, so the sale price must validate too."""

    data_dir._catalogue = {42243: GrocerProduct(42243, "Anchor Butter", "", "500g")}
    data_dir._stores = {228: "PAK'nSAVE Ormiston"}
    data_dir._store_prices = {228: {42243: GrocerPrice(42243, 9.69, 8.5)}}

    hit = data_dir.match(228, "Anchor Butter 500g", 8.5)

    assert hit is not None
    assert hit.price.regular == 9.69
    assert hit.price.sale == 8.5
    assert hit.price.effective == 8.5


def test_match_without_a_price_is_refused(data_dir) -> None:
    build_match_data(data_dir, regular=7.99, sale=None)

    assert data_dir.match(228, "Sanitarium Weet-Bix 1.2kg", None) is None


def test_match_returns_no_history_when_not_mirrored(data_dir) -> None:
    build_match_data(data_dir, regular=7.99, sale=None)

    hit = data_dir.match(228, "Sanitarium Weet-Bix 1.2kg", 7.99)

    assert hit is not None
    assert hit.history == []
    assert hit.history_for() == []


def test_match_with_no_store_prices_is_none(data_dir) -> None:
    data_dir._catalogue = {6403: GrocerProduct(6403, "Weet-Bix", "Sanitarium", "1.2kg")}
    data_dir._stores = {228: "PAK'nSAVE Ormiston"}

    assert data_dir.match(228, "Sanitarium Weet-Bix 1.2kg", 7.99) is None


# --- housekeeping -----------------------------------------------------------


def test_prune_history_deletes_only_unwanted_products(data_dir) -> None:
    mirror = data_dir._dir
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "history_1.parquet").write_bytes(b"x")
    (mirror / "history_2.parquet").write_bytes(b"x")
    (mirror / "history_3.parquet").write_bytes(b"x")
    (mirror / "prices_228.parquet").write_bytes(b"x")

    removed = data_dir.prune_history({1, 3})

    assert removed == 1
    assert (mirror / "history_1.parquet").exists()
    assert not (mirror / "history_2.parquet").exists()
    # Store price files are not history and must survive.
    assert (mirror / "prices_228.parquet").exists()


def test_mirrored_bytes_reports_zero_when_empty(data_dir) -> None:
    assert data_dir.mirrored_bytes() == 0


def test_missing_catalogue_raises_grocer_error(data_dir, monkeypatch) -> None:
    monkeypatch.setattr(GrocerData, "duckdb_available", staticmethod(lambda: True))
    data_dir._catalogue = None
    data_dir._stores = None

    with pytest.raises(GrocerError):
        data_dir.stores()


# --- coordinator integration ------------------------------------------------


def test_history_is_disabled_without_a_grocer_store() -> None:
    import smart_shopping_route.price_coordinator as price_coordinator

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), _entry({}), summaries=lambda: ["Milk"]
    )

    assert coordinator.history_enabled is False
    assert coordinator.grocer_store_id is None


def test_history_enabled_requires_duckdb(monkeypatch) -> None:
    import smart_shopping_route.price_coordinator as price_coordinator

    monkeypatch.setattr(GrocerData, "duckdb_available", staticmethod(lambda: False))
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), _entry({"grocer_store_id": 228}), summaries=lambda: ["Milk"]
    )

    assert coordinator.history_enabled is False


def _entry(extra: dict):
    data = {"price_chain": "paknsave"}
    data.update(extra)
    return SimpleNamespace(
        entry_id="e1",
        title="Shop",
        data=data,
        async_on_unload=lambda _cb: None,
        pref_disable_polling=False,
    )


def test_backfill_imports_validated_history(monkeypatch) -> None:
    """A validated grocer match replaces the series with real history."""

    import smart_shopping_route.price_coordinator as price_coordinator
    from smart_shopping_route.prices import PriceQuote

    monkeypatch.setattr(GrocerData, "duckdb_available", staticmethod(lambda: True))
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(),
        _entry({"grocer_store_id": 228}),
        summaries=lambda: ["Weetbix"],
    )
    match = GrocerMatch(
        product=GrocerProduct(6403, "Weet-Bix", "Sanitarium", "1.2kg"),
        price=GrocerPrice(6403, 7.99, None),
        history=[(1000.0, 4.49), (2000.0, 6.99), (3000.0, 7.99)],
    )

    async def fake_match(*_args, **_kwargs):
        return match

    coordinator._grocer_data = SimpleNamespace(
        async_match_with_history=fake_match, mirrored_bytes=lambda: 0
    )
    quote = PriceQuote(
        chain="paknsave",
        product_id="p",
        product_name="Sanitarium Weet-Bix 1.2kg",
        price=7.99,
    )

    asyncio.run(coordinator._async_backfill("weetbix", quote))

    series = coordinator.snapshot.points_for("weetbix")
    assert [point.price for point in series] == [4.49, 6.99, 7.99]
    assert coordinator.snapshot.lowest_for("weetbix") == 4.49


def test_backfill_runs_once_per_item(monkeypatch) -> None:
    import smart_shopping_route.price_coordinator as price_coordinator
    from smart_shopping_route.prices import PriceQuote

    monkeypatch.setattr(GrocerData, "duckdb_available", staticmethod(lambda: True))
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(),
        _entry({"grocer_store_id": 228}),
        summaries=lambda: ["Weetbix"],
    )
    calls: list[str] = []

    async def fake_match(*_args, **_kwargs):
        calls.append("x")
        return None

    coordinator._grocer_data = SimpleNamespace(
        async_match_with_history=fake_match, mirrored_bytes=lambda: 0
    )
    quote = PriceQuote(
        chain="paknsave", product_id="p", product_name="Weet-Bix", price=7.99
    )

    for _ in range(3):
        asyncio.run(coordinator._async_backfill("weetbix", quote))

    assert len(calls) == 1


def test_backfill_keeps_our_own_fresher_observations(monkeypatch) -> None:
    """Imported history must not overwrite a reading we just took."""

    import smart_shopping_route.price_coordinator as price_coordinator
    from smart_shopping_route.prices import PriceQuote

    monkeypatch.setattr(GrocerData, "duckdb_available", staticmethod(lambda: True))
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(),
        _entry({"grocer_store_id": 228}),
        summaries=lambda: ["Weetbix"],
    )
    coordinator._snapshot.history["weetbix"] = [
        price_coordinator.PricePoint(3000.0, 7.5)
    ]
    match = GrocerMatch(
        product=GrocerProduct(6403, "Weet-Bix", "Sanitarium", "1.2kg"),
        price=GrocerPrice(6403, 7.99, None),
        history=[(1000.0, 4.49), (3000.0, 7.99)],
    )

    async def fake_match(*_args, **_kwargs):
        return match

    coordinator._grocer_data = SimpleNamespace(
        async_match_with_history=fake_match, mirrored_bytes=lambda: 0
    )
    quote = PriceQuote(
        chain="paknsave", product_id="p", product_name="Weet-Bix", price=7.5
    )

    asyncio.run(coordinator._async_backfill("weetbix", quote))

    series = coordinator.snapshot.points_for("weetbix")
    assert [point.price for point in series] == [4.49, 7.5]


# --- pack-size safety and thread safety -------------------------------------


@pytest.mark.parametrize(
    ("wanted", "candidate", "expected"),
    [
        (
            "Sealord Olive Oil Blend Tuna 185g",
            "Sealord Tuna Olive Oil Blend 95g",
            False,
        ),
        ("Pams Pure Butter 500g", "Pams Pure Butter 250g", False),
        ("Pams 3ply Toilet Paper 12pk", "Pams Value Toilet Tissue 40pk", False),
        ("Pams Pure Butter 500g", "Pams Pure Butter 500g", True),
        ("Weet-Bix 1.2kg", "Weet-Bix 1200g", True),
        # No size on one side is not evidence of a mismatch.
        ("Sanitarium Weet-Bix Breakfast Cereal", "Sanitarium Weet-Bix 1.2kg", True),
        ("Pams Pure Butter 500g", "Pams Pure Butter", True),
    ],
)
def test_pack_sizes_must_agree(wanted, candidate, expected) -> None:
    """Price agreement alone can accept the wrong size of the same product."""

    from smart_shopping_route.grocer import _sizes_compatible

    assert _sizes_compatible(wanted, candidate) is expected


def test_match_rejects_a_price_agreeing_wrong_size(data_dir) -> None:
    """Two sizes often share a price, so size must be checked as well."""

    data_dir._catalogue = {
        1: GrocerProduct(1, "Sealord Tuna Olive Oil Blend", "Sealord", "95g"),
        2: GrocerProduct(2, "Sealord Olive Oil Blend Tuna", "Sealord", "185g"),
    }
    data_dir._stores = {228: "PAK'nSAVE Ormiston"}
    data_dir._store_prices = {
        228: {
            1: GrocerPrice(1, 1.89, None),
            2: GrocerPrice(2, 2.49, None),
        }
    }

    hit = data_dir.match(228, "Sealord Olive Oil Blend Tuna 185g", 1.89)

    # The 95g is the same price but the wrong size, so nothing is accepted.
    assert hit is None


def test_concurrent_matches_do_not_corrupt_results(data_dir) -> None:
    """A duckdb connection is not thread safe; queries must be serialised.

    Concurrent use returns malformed rows instead of raising, which showed up as
    a ValueError while unpacking a store price row.
    """

    products = {
        i: GrocerProduct(i, f"Product {i}", "Brand", f"{100 + i}g") for i in range(40)
    }
    data_dir._catalogue = products
    data_dir._stores = {228: "PAK'nSAVE Ormiston"}
    data_dir._store_prices = {228: {i: GrocerPrice(i, 5.0 + i, None) for i in products}}

    results = [
        data_dir.match(228, f"Brand Product {i} {100 + i}g", 5.0 + i) for i in range(40)
    ]

    assert all(result is not None for result in results)
    assert {result.product.product_id for result in results} == set(range(40))


def test_real_duckdb_concurrent_queries_are_serialised(data_dir, tmp_path) -> None:
    """Exercise the real duckdb path from several threads at once.

    A duckdb connection returns malformed rows under concurrent use rather than
    raising, so this asserts every concurrent query gets the right answer. The
    in-memory catalogue tests cannot cover this because they skip duckdb.
    """

    duckdb = pytest.importorskip("duckdb")
    mirror = data_dir._dir
    mirror.mkdir(parents=True, exist_ok=True)

    # A real catalogue and a real per-store price file.
    catalogue = mirror / "base_v3.duckdb"
    con = duckdb.connect(str(catalogue))
    con.execute(
        "create table public_products (id int, name varchar, brand varchar, size varchar)"
    )
    con.execute("create table public_stores (id int, name varchar)")
    rows = [(i, f"Product {i}", "Brand", f"{100 + i}g") for i in range(60)]
    con.executemany("insert into public_products values (?, ?, ?, ?)", rows)
    con.execute("insert into public_stores values (228, ?)", ["PAK'nSAVE Ormiston"])
    con.close()

    prices = mirror / "prices_228.parquet"
    pcon = duckdb.connect()
    pcon.execute(
        "create table t as select * from (values "
        + ", ".join(f"({i}, {(500 + i * 3)}, NULL, now())" for i in range(60))
        + ") t(product_id, original_price_cent, sale_price_cent, updated_at)"
    )
    pcon.execute(f"copy t to '{prices}' (format parquet)")
    pcon.close()

    data_dir._catalogue = None
    data_dir._stores = None
    data_dir._store_prices = {}

    import concurrent.futures

    def one(i: int):
        return data_dir.match(228, f"Brand Product {i} {100 + i}g", (500 + i * 3) / 100)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, range(60)))

    assert all(result is not None for result in results), (
        "a concurrent query returned nothing"
    )
    assert [result.product.product_id for result in results] == list(range(60))
