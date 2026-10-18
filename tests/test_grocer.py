"""grocer.nz data: parquet reading, mirroring, and self-validating matching.

The integration reads grocer.nz Parquet files with its own reader because no
third-party parquet library has wheels for Home Assistant OS, and a failed
requirement stops the whole integration loading.
"""

import asyncio
from types import SimpleNamespace

import pytest

import smart_shopping_route.price_coordinator as price_coordinator
from smart_shopping_route.grocer import (
    GrocerData,
    GrocerError,
    GrocerMatch,
    GrocerPrice,
    GrocerProduct,
    _normalise,
    _sizes_compatible,
)


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


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> GrocerData:
    # HTTP streaming fixtures use marker bytes; reference-format validation has
    # separate tests with real Parquet and the real validator.
    monkeypatch.setattr(
        GrocerData, "_validate_mirror", staticmethod(lambda *_args: None)
    )

    async def run_in_thread(func, *args):
        return func(*args)

    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda p: str(tmp_path / p)),
        async_add_executor_job=run_in_thread,
    )
    data = GrocerData(hass)
    data._session = None
    return data


# --- normalisation ----------------------------------------------------------


def test_normalise_folds_punctuation_and_case() -> None:
    assert _normalise("PAK'nSAVE Ormiston") == "paknsave ormiston"
    assert _normalise("PakNSave  Ormiston") == "paknsave ormiston"
    assert _normalise(None) == ""
    assert _normalise(float("nan")) == ""


@pytest.mark.parametrize(
    ("wanted", "candidate", "expected"),
    [
        (
            "Sealord Olive Oil Blend Tuna 185g",
            "Sealord Tuna Olive Oil Blend 95g",
            False,
        ),
        ("Pams Pure Butter 500g", "Pams Pure Butter 250g", False),
        ("Pams Pure Butter 500g", "Pams Pure Butter 500g", True),
        ("Weet-Bix 1.2kg", "Weet-Bix 1200g", True),
        # No size on one side is not evidence of a mismatch.
        ("Sanitarium Weet-Bix Breakfast Cereal", "Sanitarium Weet-Bix 1.2kg", True),
    ],
)
def test_pack_sizes_must_agree(wanted, candidate, expected) -> None:
    """Price agreement alone can accept the wrong size of the same product."""

    assert _sizes_compatible(wanted, candidate) is expected


# --- mirror safety ----------------------------------------------------------


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


def test_store_prices_are_mirrored_atomically(data_dir) -> None:
    data_dir._session = FakeSession([FakeResponse(200, b"parquet-bytes")])

    ok = asyncio.run(data_dir.async_sync_store_prices(228))

    assert ok is True
    assert (data_dir._dir / "prices_228.parquet").read_bytes() == b"parquet-bytes"
    assert not list(data_dir._dir.glob("*.part"))


def test_failed_download_keeps_the_existing_mirror(data_dir) -> None:
    """The mirror exists so an outage does not lose downloaded history."""

    target = data_dir._dir / "prices_228.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"old-good-copy")
    data_dir._session = FakeSession([FakeResponse(503)])

    assert asyncio.run(data_dir.async_sync_store_prices(228)) is False
    assert target.read_bytes() == b"old-good-copy"


def test_partial_download_is_discarded(data_dir) -> None:
    target = data_dir._dir / "prices_228.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"good")

    class Broken(FakeSession):
        def get(self, url, **_kwargs):
            self.urls.append(url)
            return _BrokenResponse()

    data_dir._session = Broken([])

    assert asyncio.run(data_dir.async_sync_store_prices(228)) is False
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


def test_concurrent_downloads_do_not_collide(tmp_path) -> None:
    """Locks are per instance but validated replacements are atomic."""
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")

    async def run_in_thread(func, *args):
        return func(*args)

    def make() -> GrocerData:
        hass = SimpleNamespace(
            config=SimpleNamespace(path=lambda p: str(tmp_path / p)),
            async_add_executor_job=run_in_thread,
        )
        data = GrocerData(hass)
        buffer = pa.BufferOutputStream()
        pq.write_table(
            pa.table({
                "product_id": [id(data)],
                "original_price_cent": [400],
                "sale_price_cent": [None],
                "updated_at": ["2026-01-01"],
            }),
            buffer,
            compression="gzip",
        )
        data._session = SlowSession(buffer.getvalue().to_pybytes())
        return data

    first, second = make(), make()

    async def both():
        return await asyncio.gather(
            first.async_sync_store_prices(228), second.async_sync_store_prices(228)
        )

    assert asyncio.run(both()) == [True, True]
    assert not list(first._dir.glob("*.part"))


class SlowSession:
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


def test_prune_history_spares_products_still_on_a_list(data_dir) -> None:
    mirror = data_dir._dir
    mirror.mkdir(parents=True, exist_ok=True)
    for pid in (1, 2, 3):
        (mirror / f"history_{pid}.parquet").write_bytes(b"x")

    removed = data_dir.prune_history({1}, maximum=1)

    assert removed == 2
    assert (mirror / "history_1.parquet").exists()
    assert not (mirror / "history_2.parquet").exists()
    # Store price files are not history and must survive pruning.
    assert not list(mirror.glob("prices_*.parquet"))


def test_mirrored_bytes_reports_zero_when_empty(data_dir) -> None:
    async def run():
        return await data_dir.async_mirrored_bytes()

    assert asyncio.run(run()) == 0


# --- matching ---------------------------------------------------------------


def build_match_data(data_dir, *, regular: float, sale: float | None) -> None:
    data_dir._store_prices = {
        228: {
            6403: GrocerPrice(6403, regular, sale),
            111: GrocerPrice(111, 4.59, None),
        }
    }


NAMES = {
    6403: "sanitarium weet bix 1.2kg",
    111: "sanitarium weet bix",
    42243: "anchor butter 500g",
}


def _fake_names(_self):
    return dict(NAMES)


def test_match_requires_price_agreement(data_dir, monkeypatch) -> None:
    """A name match alone is not trusted; the price must agree."""

    build_match_data(data_dir, regular=7.99, sale=None)
    monkeypatch.setattr(GrocerData, "names", _fake_names)

    hit = asyncio.run(data_dir.async_match(228, "Sanitarium Weet-Bix 1.2kg", 7.99))
    miss = asyncio.run(data_dir.async_match(228, "Sanitarium Weet-Bix 1.2kg", 12.5))

    assert hit is not None
    assert hit.product.product_id == 6403
    assert miss is None


def test_match_accepts_the_sale_price(data_dir, monkeypatch) -> None:
    """Retailers quote the special price, so the sale price must validate too."""

    data_dir._store_prices = {228: {42243: GrocerPrice(42243, 9.69, 8.5)}}
    monkeypatch.setattr(GrocerData, "names", _fake_names)

    hit = asyncio.run(data_dir.async_match(228, "Anchor Butter 500g", 8.5))

    assert hit is not None
    assert hit.price.regular == 9.69
    assert hit.price.effective == 8.5


def test_match_rejects_a_wrong_product_that_shares_words(data_dir, monkeypatch) -> None:
    """Name similarity alone is not enough; the price must agree too."""

    data_dir._store_prices = {228: {1: GrocerPrice(1, 13.99, None)}}
    monkeypatch.setattr(
        GrocerData, "names", lambda _self: {1: "pams extra virgin olive oil"}
    )

    assert (
        asyncio.run(
            data_dir.async_match(228, "Sealord Olive Oil Blend Tuna 185g", 1.89)
        )
        is None
    )


def test_match_only_considers_products_the_store_stocks(data_dir, monkeypatch) -> None:
    """Candidates come from the store's own price file, not the whole catalogue."""

    data_dir._store_prices = {85: {6403: GrocerPrice(6403, 7.99, None)}}
    monkeypatch.setattr(
        GrocerData, "names", lambda _self: {6403: "sanitarium weet bix"}
    )

    assert (
        asyncio.run(data_dir.async_match(228, "Sanitarium Weet-Bix 1.2kg", 7.99))
        is None
    )


def test_match_without_a_price_is_refused(data_dir, monkeypatch) -> None:
    build_match_data(data_dir, regular=7.99, sale=None)
    monkeypatch.setattr(GrocerData, "names", _fake_names)

    assert (
        asyncio.run(data_dir.async_match(228, "Sanitarium Weet-Bix 1.2kg", None))
        is None
    )


def test_match_with_no_store_prices_is_none(data_dir, monkeypatch) -> None:
    monkeypatch.setattr(GrocerData, "names", _fake_names)

    assert (
        asyncio.run(data_dir.async_match(228, "Sanitarium Weet-Bix 1.2kg", 7.99))
        is None
    )


# --- coordinator integration ------------------------------------------------


def test_history_is_disabled_without_a_grocer_store() -> None:
    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), _entry({}), summaries=lambda: ["Milk"]
    )

    assert coordinator.history_enabled is False
    assert coordinator.grocer_store_id is None


def test_backfill_imports_validated_history(monkeypatch) -> None:
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
    quote = price_coordinator.PriceQuote(
        chain="paknsave", product_id="p", product_name="Weet-Bix", price=7.99
    )

    asyncio.run(coordinator._async_backfill("weetbix", quote))

    series = coordinator.snapshot.points_for("weetbix")
    assert [point.price for point in series] == [4.49, 6.99, 7.99]
    assert coordinator.snapshot.lowest_for("weetbix") == 4.49


def test_missing_mirror_or_match_is_not_permanent() -> None:
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
    quote = price_coordinator.PriceQuote(
        chain="paknsave", product_id="p", product_name="Weet-Bix", price=7.99
    )

    for _ in range(3):
        asyncio.run(coordinator._async_backfill("weetbix", quote))

    assert len(calls) == 3
    assert "weetbix" not in coordinator._backfilled


def test_backfill_keeps_our_own_fresher_observations() -> None:
    """Imported history must not overwrite a reading we just took."""

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
    quote = price_coordinator.PriceQuote(
        chain="paknsave", product_id="p", product_name="Weet-Bix", price=7.5
    )

    asyncio.run(coordinator._async_backfill("weetbix", quote))

    assert [p.price for p in coordinator.snapshot.points_for("weetbix")] == [4.49, 7.5]


def test_transient_backfill_failure_is_retried() -> None:
    """A network failure must not permanently skip an item until restart."""

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), _entry({"grocer_store_id": 228}), summaries=lambda: ["Milk"]
    )
    attempts: list[int] = []

    async def flaky(*_args, **_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise GrocerError("temporarily down")
        return None

    coordinator._grocer_data = SimpleNamespace(
        async_match_with_history=flaky, mirrored_bytes=lambda: 0
    )
    quote = price_coordinator.PriceQuote(
        chain="paknsave", product_id="p", product_name="Milk 2L", price=4.0
    )

    asyncio.run(coordinator._async_backfill("milk", quote))
    assert "milk" not in coordinator._backfilled

    asyncio.run(coordinator._async_backfill("milk", quote))
    assert len(attempts) == 2
    assert "milk" not in coordinator._backfilled


def test_grocer_sync_timestamps_are_persisted() -> None:
    """Otherwise a profile with no matches re-downloads on every restart."""

    async def run_in_thread(func, *args):
        return func(*args)

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(async_add_executor_job=run_in_thread),
        _entry({"grocer_store_id": 228}),
        summaries=lambda: ["Milk"],
    )

    async def ok(*_args, **_kwargs):
        return True

    async def zero():
        return 0

    async def names_ok():
        return True

    coordinator._grocer_data = SimpleNamespace(
        async_sync_names=names_ok,
        async_sync_store_prices=ok,
        async_mirrored_bytes=zero,
        prune_history=lambda keep, maximum: 0,
    )

    asyncio.run(coordinator.async_sync_grocer(force=True))

    saved = coordinator._price_store.saved
    assert saved, "sync timestamps were not persisted"
    assert saved[-1]["grocer"]["store_at"] > 0


def test_manifest_has_no_compiled_requirements() -> None:
    """A compiled dependency must not be a hard requirement.

    On Home Assistant OS (Alpine/musl) no manylinux wheel matches, so pip builds
    from source, fails, and Home Assistant then refuses to load the whole
    integration: the route and classification stop working because an optional
    history feature could not install.
    """

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

    assert "requirements" not in manifest


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


# --- product names from the sitemap ----------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://grocer.nz/product/6403/sanitarium-weet-bix-breakfast-cereal",
            (6403, "sanitarium weet bix breakfast cereal"),
        ),
        (
            "https://grocer.nz/product/1/loose-leaf-tea-english-breakfast-125g",
            (1, "loose leaf tea english breakfast 125g"),
        ),
        ("https://grocer.nz/", None),
        ("https://grocer.nz/product/abc/thing", None),
        ("https://grocer.nz/product/12/", None),
    ],
)
def test_product_url_parsing(url, expected) -> None:
    from smart_shopping_route.grocer import _parse_product_url

    assert _parse_product_url(url) == expected


class TextSession:
    """Serves sitemap text for the name index."""

    def __init__(self, pages: list[str]) -> None:
        self.pages = list(pages)
        self.urls: list[str] = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        body = self.pages.pop(0) if self.pages else ""
        return _TextResponse(body)


class _TextResponse:
    def __init__(self, body: str) -> None:
        self.status = 200
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def text(self):
        return self._body


def test_names_are_built_from_the_sitemaps(data_dir) -> None:
    pages = [
        "https://grocer.nz/product/6403/sanitarium-weet-bix\nhttps://grocer.nz/\n",
        "https://grocer.nz/product/42243/pure-butter\n",
        "https://grocer.nz/product/101540/sealord-tuna-olive-oil-blend\n",
    ]
    data_dir._session = TextSession(pages)

    assert asyncio.run(data_dir.async_sync_names()) is True

    names = data_dir.names()
    assert names[6403] == "sanitarium weet bix"
    assert names[42243] == "pure butter"
    assert names[101540] == "sealord tuna olive oil blend"
    assert len(names) == 3


def test_names_are_empty_when_not_mirrored(data_dir) -> None:
    assert data_dir.names() == {}


def test_names_sync_fails_without_any_products(data_dir) -> None:
    data_dir._session = TextSession(["https://grocer.nz/\n", "", ""])

    assert asyncio.run(data_dir.async_sync_names()) is False
    assert data_dir.names() == {}


def test_names_tolerate_a_bad_id_line(data_dir) -> None:
    target = data_dir._dir / "names.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("6403\tsanitarium weet bix\nnope\tbroken\n\n", encoding="utf-8")

    assert data_dir.names() == {6403: "sanitarium weet bix"}


def test_match_uses_sitemap_names(data_dir, monkeypatch) -> None:
    """End to end: names from the sitemap, prices from the store file."""

    data_dir._store_prices = {228: {6403: GrocerPrice(6403, 7.99, None)}}
    target = data_dir._dir / "names.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("6403\tsanitarium weet bix 1.2kg\n", encoding="utf-8")

    hit = asyncio.run(data_dir.async_match(228, "Sanitarium Weet-Bix 1.2kg", 7.99))

    assert hit is not None
    assert hit.product.product_id == 6403
    assert hit.product.name == "sanitarium weet bix 1.2kg"


def test_match_is_skipped_when_names_are_unavailable(data_dir, monkeypatch) -> None:
    data_dir._store_prices = {228: {6403: GrocerPrice(6403, 7.99, None)}}
    monkeypatch.setattr(GrocerData, "names", lambda _self: {})

    assert (
        asyncio.run(data_dir.async_match(228, "Sanitarium Weet-Bix 1.2kg", 7.99))
        is None
    )


def test_pinned_grocer_product_uses_that_history(data_dir) -> None:
    """The escape hatch for variants slugs cannot distinguish."""

    data_dir._store_prices = {228: {6403: GrocerPrice(6403, 7.99, None)}}
    names = data_dir._dir / "names.txt"
    names.parent.mkdir(parents=True, exist_ok=True)
    names.write_text("6403\tsanitarium weet bix\n", encoding="utf-8")

    async def history_synced(_product_id):
        return True

    data_dir.async_sync_history = history_synced  # type: ignore[method-assign]
    data_dir._history_override = [(1000.0, 5.0), (2000.0, 7.99)]

    def fake_history(product_id, store_id):
        return list(data_dir._history_override) if product_id == 6403 else []

    data_dir.history = fake_history  # type: ignore[method-assign]

    match = asyncio.run(data_dir.async_pinned_match(228, 6403))

    assert match is not None
    assert match.product.product_id == 6403
    assert [price for _ts, price in match.history] == [5.0, 7.99]


def test_pinned_grocer_product_must_be_stocked(data_dir) -> None:
    data_dir._store_prices = {228: {}}

    assert asyncio.run(data_dir.async_pinned_match(228, 999)) is None


def test_backfill_honours_a_grocer_pin() -> None:
    """A pinned grocer product bypasses automatic matching entirely."""

    coordinator = price_coordinator.PriceCoordinator(
        SimpleNamespace(), _entry({"grocer_store_id": 228}), summaries=lambda: ["Flora"]
    )
    called: list[int] = []

    async def pinned(_store_id, product_id):
        called.append(product_id)
        return GrocerMatch(
            product=GrocerProduct(product_id, "flora pro activ buttery spread"),
            price=GrocerPrice(product_id, 8.79, None),
            history=[(1000.0, 7.39), (2000.0, 9.79)],
        )

    async def auto(*_args, **_kwargs):
        raise AssertionError("automatic matching must not run for a pinned item")

    coordinator._grocer_data = SimpleNamespace(
        async_pinned_match=pinned,
        async_match_with_history=auto,
        mirrored_bytes=lambda: 0,
    )
    coordinator._pins["flora"] = price_coordinator.ProductPin(
        chain="paknsave", grocer_product_id=10749
    )
    quote = price_coordinator.PriceQuote(
        chain="paknsave",
        product_id="p",
        product_name="Flora ProActiv Buttery",
        price=8.79,
    )

    asyncio.run(coordinator._async_backfill("flora", quote))

    assert called == [10749]
    assert [p.price for p in coordinator.snapshot.points_for("flora")] == [7.39, 9.79]
