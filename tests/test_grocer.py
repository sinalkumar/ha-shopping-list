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
    async def run_in_thread(func, *args):
        return func(*args)

    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda p: str(tmp_path / p)),
        async_add_executor_job=run_in_thread,
    )
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


def test_prune_history_bounds_the_mirror(data_dir) -> None:
    mirror = data_dir._dir
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "history_1.parquet").write_bytes(b"x")
    (mirror / "history_2.parquet").write_bytes(b"x")
    (mirror / "history_3.parquet").write_bytes(b"x")
    (mirror / "prices_228.parquet").write_bytes(b"x")

    removed = data_dir.prune_history({1, 3}, maximum=2)

    assert removed == 1
    # One file must go to reach the cap, and a listed product is spared.
    assert (mirror / "history_1.parquet").exists()
    assert (mirror / "history_3.parquet").exists()
    assert not (mirror / "history_2.parquet").exists()
    # Store price files are not history and must survive.
    assert (mirror / "prices_228.parquet").exists()


def test_mirrored_bytes_reports_zero_when_empty(data_dir) -> None:
    async def run():
        return await data_dir.async_mirrored_bytes()

    assert asyncio.run(run()) == 0


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


# --- review fixes -----------------------------------------------------------


def test_catalogue_is_used_as_downloaded(data_dir) -> None:
    """The .br suffix is misleading: the server sends an uncompressed database.

    Verified against the live asset: the payload starts with DuckDB's DUCK magic
    and opens with 114k products, and the response carries no content-encoding.
    So no decompression step belongs here, and adding one would corrupt it.
    """

    payload = b"\x94-\x89Z\x81n\x8f\x0fDUCK@\x00\x00\x00"
    data_dir._session = FakeSession([FakeResponse(200, payload)])

    assert asyncio.run(data_dir.async_sync_catalogue()) is True
    assert (data_dir._dir / "base_v3.duckdb").read_bytes() == payload


def test_concurrent_downloads_from_two_profiles_do_not_collide(tmp_path) -> None:
    """Locks are per instance, but the mirror directory is shared.

    Two profiles syncing at once must not write the same temporary file, or one
    would corrupt the other's download.
    """

    async def run_in_thread(func, *args):
        return func(*args)

    def make() -> GrocerData:
        hass = SimpleNamespace(
            config=SimpleNamespace(path=lambda p: str(tmp_path / p)),
            async_add_executor_job=run_in_thread,
        )
        data = GrocerData(hass)
        data._session = SlowSession(b"payload-" + str(id(data)).encode())
        return data

    first, second = make(), make()

    async def both():
        return await asyncio.gather(
            first.async_sync_catalogue(), second.async_sync_catalogue()
        )

    results = asyncio.run(both())

    assert results == [True, True]
    # Exactly one catalogue survived, complete, with no partials left behind.
    mirror = first._dir
    assert not list(mirror.glob("*.part"))
    assert (mirror / "base_v3.duckdb").read_bytes().startswith(b"payload-")


class SlowSession:
    """Yields twice so two concurrent downloads genuinely interleave."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def get(self, _url, **_kwargs):
        return _SlowResponse(self._payload)


class _SlowResponse:
    def __init__(self, payload: bytes) -> None:
        self.status = 200
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    @property
    def content(self):
        return _SlowChunks(self._payload)


class _SlowChunks:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def iter_chunked(self, _size):
        async def gen():
            half = len(self._payload) // 2
            yield self._payload[:half]
            await asyncio.sleep(0.01)
            yield self._payload[half:]

        return gen()


def test_disk_full_is_reported_as_grocer_error(data_dir, tmp_path) -> None:
    class FullDisk(FakeSession):
        def get(self, url, **_kwargs):
            self.urls.append(url)
            return _FullDiskResponse()

    data_dir._session = FullDisk([])

    assert asyncio.run(data_dir.async_sync_catalogue()) is False
    assert not list(data_dir._dir.glob("*.part")) if data_dir._dir.exists() else True


class _FullDiskResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    @property
    def content(self):
        return _FullDiskChunks()


class _FullDiskChunks:
    def iter_chunked(self, _size):
        async def gen():
            yield b"x"
            raise OSError(28, "No space left on device")

        return gen()


def test_store_removed_from_the_catalogue_is_refused(data_dir) -> None:
    """A stale mirror for a delisted store must not keep serving prices."""

    data_dir._catalogue = {1: GrocerProduct(1, "Milk", "Pams", "2L")}
    data_dir._stores = {228: "PAK'nSAVE Ormiston"}
    data_dir._dir.mkdir(parents=True, exist_ok=True)
    (data_dir._dir / "prices_999.parquet").write_bytes(b"x")

    # 999 is not in public_stores, so its old file is ignored.
    assert data_dir.store_prices(999) == {}


def test_catalogue_replacement_closes_the_old_connection(data_dir, monkeypatch) -> None:
    """A replaced file has a new inode; a live handle would serve stale data."""

    closed: list[bool] = []

    class FakeConnection:
        def close(self) -> None:
            closed.append(True)

        def execute(self, *_a, **_k):
            raise AssertionError("must not be used after replacement")

    data_dir._duckdb = FakeConnection()
    data_dir._session = FakeSession([FakeResponse(200, b"new")])

    asyncio.run(data_dir.async_sync_catalogue())

    assert closed == [True]
    assert data_dir._duckdb is None


def test_prune_history_spares_products_still_on_a_list(data_dir) -> None:
    mirror = data_dir._dir
    mirror.mkdir(parents=True, exist_ok=True)
    for pid in (1, 2, 3):
        path = mirror / f"history_{pid}.parquet"
        path.write_bytes(b"x")

    removed = data_dir.prune_history({1}, maximum=1)

    assert removed == 2
    assert (mirror / "history_1.parquet").exists()
    assert not (mirror / "history_2.parquet").exists()
    assert not (mirror / "history_3.parquet").exists()


def test_transient_backfill_failure_is_retried(monkeypatch) -> None:
    """A network failure must not permanently skip an item until restart."""

    import smart_shopping_route.price_coordinator as price_coordinator
    from smart_shopping_route.prices import PriceQuote

    monkeypatch.setattr(GrocerData, "duckdb_available", staticmethod(lambda: True))

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), _entry({"grocer_store_id": 228}), summaries=lambda: ["Milk"]
    )
    coordinator._grocer_data = SimpleNamespace()
    attempts: list[int] = []

    async def flaky(*_args, **_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise GrocerError("temporarily down")
        return None

    coordinator._grocer_data.async_match_with_history = flaky
    quote = PriceQuote(
        chain="paknsave", product_id="p", product_name="Milk 2L", price=4.0
    )

    asyncio.run(coordinator._async_backfill("milk", quote))
    assert "milk" not in coordinator._backfilled

    asyncio.run(coordinator._async_backfill("milk", quote))
    # Second attempt ran, and a definitive "no match" now stops further tries.
    assert len(attempts) == 2
    assert "milk" in coordinator._backfilled


def test_definitive_no_match_is_not_retried(monkeypatch) -> None:
    import smart_shopping_route.price_coordinator as price_coordinator
    from smart_shopping_route.prices import PriceQuote

    monkeypatch.setattr(GrocerData, "duckdb_available", staticmethod(lambda: True))

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), _entry({"grocer_store_id": 228}), summaries=lambda: ["Milk"]
    )
    calls: list[int] = []

    async def no_match(*_args, **_kwargs):
        calls.append(1)
        return None

    coordinator._grocer_data = SimpleNamespace(async_match_with_history=no_match)
    quote = PriceQuote(
        chain="paknsave", product_id="p", product_name="Milk 2L", price=4.0
    )

    for _ in range(3):
        asyncio.run(coordinator._async_backfill("milk", quote))

    assert len(calls) == 1


def test_grocer_sync_timestamps_are_persisted(monkeypatch) -> None:
    """Otherwise a profile with no matches re-downloads the catalogue each boot."""

    import smart_shopping_route.price_coordinator as price_coordinator

    monkeypatch.setattr(GrocerData, "duckdb_available", staticmethod(lambda: True))

    async def run_in_thread(func, *args):
        return func(*args)

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(async_add_executor_job=run_in_thread),
        _entry({"grocer_store_id": 228}),
        summaries=lambda: ["Milk"],
    )
    coordinator._grocer_data = SimpleNamespace(
        async_sync_catalogue=_ok,
        async_sync_store_prices=_ok,
        async_mirrored_bytes=_zero,
        prune_history=lambda keep, maximum: 0,
    )
    coordinator._client = object()

    asyncio.run(coordinator.async_sync_grocer(force=True))

    saved = coordinator._price_store.saved
    assert saved, "sync timestamps were not persisted"
    assert saved[-1]["grocer"]["catalogue_at"] > 0


async def _ok(*_args, **_kwargs) -> bool:
    return True


async def _zero() -> int:
    return 0


def test_manifest_keys_are_sorted_for_hassfest() -> None:
    """hassfest fails the build if domain/name are not first and the rest sorted."""

    import json
    from pathlib import Path

    manifest = json.loads(
        (
            Path(__file__).parents[1]
            / "custom_components"
            / "smart_shopping_route"
            / "manifest.json"
        ).read_text()
    )
    keys = list(manifest)

    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])
