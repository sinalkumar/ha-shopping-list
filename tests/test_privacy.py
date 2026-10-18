"""Behavioural privacy guardrails for diagnostics, logs, and cache state."""

import logging
from types import SimpleNamespace

from homeassistant.components.todo import TodoItem
from homeassistant.components.todo.const import TodoItemStatus

import smart_shopping_route.coordinator as coordinator_module
import smart_shopping_route.diagnostics as diagnostics_module
import smart_shopping_route.storage as storage_module
from smart_shopping_route import classifier
from smart_shopping_route.diagnostics import async_get_config_entry_diagnostics
from smart_shopping_route.models import RouteSection

PRIVATE_ITEM = "Synthetic private item"
PRIVATE_UID = "private-uid-1234"
PRIVATE_PROFILE = "Private store name"
PRIVATE_HINT = "private aisle hint"
PRIVATE_ENTITY = "todo.private_household_list"

# Every key diagnostics may return. Anything else is a potential leak.
ALLOWED_KEYS = {
    "integration_version",
    "section_count",
    "source_supported_features",
    "ai_task_supported_features",
    "cache_entry_count",
    "manual_override_count",
    "uid_override_count",
    "unclassified_count",
    "pending_count",
    "scheduled_retry_count",
    "source_available",
    "ai_available",
    "prices",
}

PRICE_KEYS = {
    "chain",
    "enabled",
    "priced_items",
    "unmatched_items",
    "scheduled_retries",
    "pinned_items",
}


class FakeStore:
    def __init__(self, _hass, _entry_id) -> None:
        pass

    async def async_load(self):
        return None

    async def async_save(self, cache) -> None:
        pass


class FakePriceCoordinator:
    enabled = False
    chain = "none"

    def __init__(self, _hass, _entry, *, summaries) -> None:
        pass

    def async_set_update_callback(self, on_update) -> None:
        self._on_update = on_update

    def _async_handle_update_done(self) -> None:
        if getattr(self, "_on_update", None):
            self._on_update()

    def async_setup(self) -> None:
        return None

    async def async_refresh(self) -> None:
        return None

    async def async_shutdown(self) -> None:
        return None

    def quote_for(self, _summary):
        return None

    def as_diagnostics(self):
        return {
            "chain": self.chain,
            "enabled": self.enabled,
            "priced_items": 0,
            "unmatched_items": 0,
            "scheduled_retries": 0,
            "pinned_items": 0,
        }

    @property
    def snapshot(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            quotes={}, failed=set(), specials=[], savings=0.0, total=0.0
        )


class FakeSource:
    entity_id = PRIVATE_ENTITY
    supported_features = 4

    def __init__(self) -> None:
        self._items = [
            TodoItem(
                uid=PRIVATE_UID,
                summary=PRIVATE_ITEM,
                status=TodoItemStatus.NEEDS_ACTION,
            )
        ]

    @property
    def available(self) -> bool:
        return True

    def items(self):
        return list(self._items)

    def subscribe(self, _listener):
        return lambda: None


def build(monkeypatch):
    monkeypatch.setattr(storage_module, "CacheStore", FakeStore)
    monkeypatch.setattr(coordinator_module, "PriceCoordinator", FakePriceCoordinator)
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", lambda *a: None)
    entry = SimpleNamespace(
        entry_id="synthetic-entry",
        title=PRIVATE_PROFILE,
        data={
            "profile_name": PRIVATE_PROFILE,
            "sections": [{"name": "Produce", "hints": PRIVATE_HINT}],
            "source_entity_id": PRIVATE_ENTITY,
            "ai_task_entity_id": "ai_task.synthetic",
        },
    )
    hass = SimpleNamespace(
        data={},
        states=SimpleNamespace(
            get=lambda _entity_id: SimpleNamespace(
                state="2026-01-01T00:00:00+00:00", attributes={"supported_features": 1}
            )
        ),
    )
    coordinator = coordinator_module.RouteCoordinator(hass, entry)
    coordinator.source = FakeSource()
    coordinator.items = coordinator.source.items()
    hass.data[coordinator_module.DOMAIN] = {entry.entry_id: coordinator}
    return hass, entry, coordinator


def test_diagnostics_expose_only_aggregate_keys(monkeypatch) -> None:
    hass, entry, _coordinator = build(monkeypatch)

    async def fake_integration(_hass, _domain):
        return SimpleNamespace(version="0.2.0")

    monkeypatch.setattr(diagnostics_module, "async_get_integration", fake_integration)

    import asyncio

    result = asyncio.run(async_get_config_entry_diagnostics(hass, entry))

    assert set(result) == ALLOWED_KEYS
    assert set(result["prices"]) == PRICE_KEYS
    serialized = repr(result)
    for private in (
        PRIVATE_ITEM,
        PRIVATE_UID,
        PRIVATE_PROFILE,
        PRIVATE_HINT,
        PRIVATE_ENTITY,
        "synthetic-entry",
    ):
        assert private not in serialized


def test_classification_failure_log_omits_item_text(monkeypatch, caplog) -> None:
    async def generate(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(classifier, "async_generate_data", generate)
    with caplog.at_level(logging.WARNING):
        import asyncio

        result = asyncio.run(
            classifier.AITaskClassifier(object(), "ai_task.synthetic").async_classify(
                PRIVATE_ITEM, (RouteSection("Produce", PRIVATE_HINT),)
            )
        )
    assert result is None
    assert PRIVATE_ITEM not in caplog.text
    assert PRIVATE_HINT not in caplog.text
    assert "failed for 1 item" in caplog.text


def test_persisted_cache_is_documented_as_containing_item_names() -> None:
    """The cache key is the normalized summary; document that it is stored."""

    import asyncio

    from smart_shopping_route.cache import ClassificationCache

    cache = ClassificationCache()
    cache.set(PRIVATE_ITEM, "Produce")

    class Store:
        def __init__(self) -> None:
            self.written = None

        async def async_save(self, data) -> None:
            self.written = data

        async def async_remove(self) -> None:
            pass

    store = Store()
    wrapper = storage_module.CacheStore.__new__(storage_module.CacheStore)
    wrapper._store = store

    asyncio.run(wrapper.async_save(cache))

    # Persisted state is local-only user data; assert it round-trips intact so a
    # future change cannot silently broaden what is written.
    assert list(store.written["entries"]) == ["synthetic private item"]


def test_no_module_logs_raw_summaries() -> None:
    """Guard the intent, not the exact string: no logger call formats a summary."""

    from pathlib import Path

    root = Path(__file__).parents[1] / "custom_components" / "smart_shopping_route"
    for path in root.glob("*.py"):
        source = path.read_text()
        for line in source.splitlines():
            stripped = line.strip()
            if "_LOGGER." not in stripped:
                continue
            assert "summary" not in stripped, f"{path.name}: {stripped}"
            assert "item.summary" not in stripped, f"{path.name}: {stripped}"


def test_logger_name_matches_manifest() -> None:
    """Debug logging instructions must match the manifest loggers entry."""

    import json
    from pathlib import Path

    root = Path(__file__).parents[1] / "custom_components" / "smart_shopping_route"
    manifest = json.loads((root / "manifest.json").read_text())

    assert manifest["loggers"] == ["custom_components.smart_shopping_route"]


def test_retry_debug_log_omits_item_text(monkeypatch, caplog) -> None:
    """Backoff logging must stay count-only, like the failure log."""

    from smart_shopping_route.coordinator import RouteCoordinator

    coordinator = RouteCoordinator.__new__(RouteCoordinator)
    coordinator._attempts = {}
    coordinator._retry_after = {}

    with caplog.at_level(logging.DEBUG):
        coordinator._schedule_retry("synthetic private item")

    assert "Scheduling classification retry 1" in caplog.text
    assert PRIVATE_ITEM not in caplog.text
    assert "synthetic private item" not in caplog.text
