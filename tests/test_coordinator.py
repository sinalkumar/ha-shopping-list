"""Coordinator lifecycle, availability, correction precedence, and AI cost."""

import asyncio
import copy
from types import SimpleNamespace

import pytest
from homeassistant.components.todo import TodoItem
from homeassistant.components.todo.const import TodoItemStatus
from homeassistant.core import CoreState

import smart_shopping_route.coordinator as coordinator_module
import smart_shopping_route.storage as storage_module
from smart_shopping_route.cache import ClassificationCache
from smart_shopping_route.coordinator import RouteCoordinator
from smart_shopping_route.models import RouteSection

SECTIONS = [
    {"name": "Produce", "hints": "fruit"},
    {"name": "Dairy", "hints": "milk"},
]


class FakeStore:
    """In-memory stand-in for CacheStore."""

    def __init__(self, _hass, _entry_id) -> None:
        self.saved: list[dict] = []
        self.removed = False

    async def async_load(self):
        return ClassificationCache()

    async def async_save(self, cache) -> None:
        self.saved.append(cache.as_dict())

    async def async_remove(self) -> None:
        self.removed = True


class FakeSource:
    entity_id = "todo.synthetic_source"
    supported_features = 4

    def __init__(self, items) -> None:
        self._items = list(items)
        self.listener = None

    @property
    def available(self) -> bool:
        return True

    def items(self) -> list[TodoItem]:
        return [copy.copy(item) for item in self._items]

    def subscribe(self, listener):
        self.listener = listener
        return lambda: None

    def push(self, items) -> None:
        self._items = list(items)
        self.listener(self.items())


class FakePriceCoordinator:
    """Route tests do not exercise pricing; price tests cover it directly."""

    enabled = False
    chain = "none"

    def __init__(self, _hass, _entry, *, summaries) -> None:
        self._summaries = summaries

    def async_setup(self) -> None:
        return None

    async def async_refresh(self) -> None:
        return None

    async def async_shutdown(self) -> None:
        return None

    def quote_for(self, _summary):
        return None

    @property
    def snapshot(self):
        return SimpleNamespace(
            quotes={}, failed=set(), specials=[], savings=0.0, total=0.0
        )


class FakeClassifier:
    """Records every provider call and replays scripted answers."""

    def __init__(self, answer=("Dairy", 0.9)) -> None:
        self.answer = answer
        self.calls: list[str] = []

    async def async_classify(self, summary, sections):
        self.calls.append(summary)
        return self.answer


def make_coordinator(items, monkeypatch, *, ai_state="unknown", answer=("Dairy", 0.9)):
    monkeypatch.setattr(storage_module, "CacheStore", FakeStore)
    monkeypatch.setattr(coordinator_module, "PriceCoordinator", FakePriceCoordinator)
    monkeypatch.setattr(coordinator_module, "async_dispatcher_send", lambda *a: None)
    entry = SimpleNamespace(
        entry_id="synthetic-entry",
        title="Example Supermarket",
        data={
            "profile_name": "Example Supermarket",
            "sections": SECTIONS,
            "source_entity_id": "todo.synthetic_source",
            "ai_task_entity_id": "ai_task.synthetic",
        },
    )
    entry.async_create_background_task = lambda _hass, coro, _name: asyncio.create_task(
        coro
    )
    scheduled: list[object] = []

    class FakeLoop:
        """Record delayed callbacks instead of scheduling them."""

        @staticmethod
        def call_later(_delay, callback):
            scheduled.append(callback)
            return SimpleNamespace(cancel=lambda: None)

    listeners: list[tuple[object, object]] = []

    class FakeBus:
        @staticmethod
        def async_listen_once(event, callback):
            listeners.append((event, callback))
            return lambda: listeners.remove((event, callback))

    hass = SimpleNamespace(
        data={},
        loop=FakeLoop(),
        scheduled=scheduled,
        bus=FakeBus(),
        final_write_listeners=listeners,
        state=SimpleNamespace(),
        states=SimpleNamespace(
            get=lambda entity_id: (
                SimpleNamespace(state=ai_state, attributes={"supported_features": 1})
                if entity_id == "ai_task.synthetic"
                else None
            )
        ),
    )
    coordinator = RouteCoordinator(hass, entry)
    coordinator.source = FakeSource(items)
    coordinator.classifier = FakeClassifier(answer)
    coordinator.items = coordinator.source.items()
    return coordinator


def item(uid: str, summary: str, status=TodoItemStatus.NEEDS_ACTION) -> TodoItem:
    return TodoItem(uid=uid, summary=summary, status=status)


async def run_classification(coordinator) -> None:
    coordinator.async_classify_unknown()
    for _ in range(5):
        tasks = list(coordinator._tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


async def push_source_update(coordinator, items) -> None:
    """Deliver a source push the way a Todo entity would, inside a loop."""

    coordinator._source_updated([copy.copy(item) for item in items])
    await run_classification(coordinator)


def test_route_is_available_while_ai_task_state_is_unknown(monkeypatch) -> None:
    """A fresh AI Task entity reports 'unknown' until its first generation."""

    coordinator = make_coordinator([], monkeypatch, ai_state="unknown")
    assert coordinator.available is True
    assert coordinator.ai_available is True


def test_unavailable_ai_task_does_not_block_the_route(monkeypatch) -> None:
    coordinator = make_coordinator([], monkeypatch, ai_state="unavailable")
    assert coordinator.available is True
    assert coordinator.ai_available is False


def test_remembered_correction_beats_earlier_uid_override(monkeypatch) -> None:
    source_item = item("uid-1", "Milk")
    coordinator = make_coordinator([source_item], monkeypatch)

    asyncio.run(coordinator.async_set_section("uid-1", "Produce", remember=False))
    assert coordinator.section_for(source_item) == "Produce"

    asyncio.run(coordinator.async_set_section("uid-1", "Dairy", remember=True))
    assert coordinator.section_for(source_item) == "Dairy"
    assert coordinator.cache.uid_overrides == {}


def test_clear_cache_including_manual_clears_uid_only_correction(monkeypatch) -> None:
    source_item = item("uid-1", "Milk")
    coordinator = make_coordinator([source_item], monkeypatch)

    asyncio.run(coordinator.async_set_section("uid-1", "Produce", remember=False))
    asyncio.run(coordinator.async_clear_cache(include_manual=True))

    assert coordinator.cache.uid_overrides == {}
    assert coordinator.section_for(source_item) == "Unclassified / Other"


def test_clear_cache_preserves_uid_corrections_by_default(monkeypatch) -> None:
    source_item = item("uid-1", "Milk")
    coordinator = make_coordinator([source_item], monkeypatch)

    asyncio.run(coordinator.async_set_section("uid-1", "Produce", remember=False))
    asyncio.run(coordinator.async_clear_cache(include_manual=False))

    assert coordinator.section_for(source_item) == "Produce"


def test_remember_requires_a_summary(monkeypatch) -> None:
    coordinator = make_coordinator([item("uid-1", "")], monkeypatch)
    with pytest.raises(ValueError):
        asyncio.run(coordinator.async_set_section("uid-1", "Dairy", remember=True))


def test_unclassifiable_answer_is_cached_and_not_repeated(monkeypatch) -> None:
    """'No section fits' is a definitive answer, not a failure to retry."""

    source_item = item("uid-1", "Cat litter")
    coordinator = make_coordinator(
        [source_item], monkeypatch, answer=("Unclassified / Other", 0.9)
    )

    asyncio.run(run_classification(coordinator))
    asyncio.run(run_classification(coordinator))
    asyncio.run(run_classification(coordinator))

    assert coordinator.classifier.calls == ["Cat litter"]
    assert coordinator.section_for(source_item) == "Unclassified / Other"
    assert coordinator.unclassified_count == 1


def test_low_confidence_answer_is_treated_as_unclassified(monkeypatch) -> None:
    source_item = item("uid-1", "Mystery sauce")
    coordinator = make_coordinator([source_item], monkeypatch, answer=("Dairy", 0.2))

    asyncio.run(run_classification(coordinator))

    assert coordinator.section_for(source_item) == "Unclassified / Other"
    assert coordinator.classifier.calls == ["Mystery sauce"]


def test_failed_classification_backs_off_instead_of_repeating(monkeypatch) -> None:
    source_item = item("uid-1", "Milk")
    coordinator = make_coordinator([source_item], monkeypatch, answer=None)

    asyncio.run(run_classification(coordinator))
    asyncio.run(run_classification(coordinator))
    asyncio.run(run_classification(coordinator))

    assert coordinator.classifier.calls == ["Milk"]
    assert coordinator.retry_scheduled_count == 1
    assert coordinator.section_for(source_item) == "Unclassified / Other"


def test_refresh_clears_backoff_and_retries(monkeypatch) -> None:
    coordinator = make_coordinator([item("uid-1", "Milk")], monkeypatch, answer=None)

    asyncio.run(run_classification(coordinator))
    asyncio.run(coordinator.async_refresh())
    asyncio.run(run_classification(coordinator))

    assert coordinator.classifier.calls == ["Milk", "Milk"]


def test_duplicate_summaries_share_one_provider_call(monkeypatch) -> None:
    items = [item("uid-1", "Milk"), item("uid-2", "MILK")]
    coordinator = make_coordinator(items, monkeypatch)

    asyncio.run(run_classification(coordinator))

    assert coordinator.classifier.calls == ["Milk"]


def test_source_update_prunes_removed_uid_overrides(monkeypatch) -> None:
    coordinator = make_coordinator([item("uid-1", "Milk")], monkeypatch)
    asyncio.run(coordinator.async_set_section("uid-1", "Produce", remember=False))
    assert coordinator.cache.uid_overrides == {"uid-1": "Produce"}

    asyncio.run(push_source_update(coordinator, [item("uid-2", "Bread")]))

    assert coordinator.cache.uid_overrides == {}


def test_pruning_schedules_a_coalesced_save(monkeypatch) -> None:
    coordinator = make_coordinator([item("uid-1", "Milk")], monkeypatch)
    asyncio.run(coordinator.async_set_section("uid-1", "Produce", remember=False))
    asyncio.run(push_source_update(coordinator, [item("uid-2", "Bread")]))

    assert len(coordinator.hass.scheduled) == 1


def test_source_update_keeps_overrides_while_the_item_exists(monkeypatch) -> None:
    coordinator = make_coordinator([item("uid-1", "Milk")], monkeypatch)
    asyncio.run(coordinator.async_set_section("uid-1", "Produce", remember=False))

    asyncio.run(
        push_source_update(coordinator, [item("uid-1", "Milk"), item("uid-2", "Bread")])
    )

    assert coordinator.cache.uid_overrides == {"uid-1": "Produce"}


def test_forget_retries_the_item(monkeypatch) -> None:
    source_item = item("uid-1", "Milk")
    coordinator = make_coordinator([source_item], monkeypatch)

    asyncio.run(run_classification(coordinator))
    assert coordinator.section_for(source_item) == "Dairy"

    asyncio.run(coordinator.async_forget("uid-1"))
    asyncio.run(run_classification(coordinator))

    assert coordinator.classifier.calls == ["Milk", "Milk"]
    assert coordinator.section_for(source_item) == "Dairy"


def test_background_classification_writes_are_coalesced(monkeypatch) -> None:
    """Many classifications produce one scheduled disk write, not one each."""

    coordinator = make_coordinator(
        [item("uid-1", "Milk"), item("uid-2", "Bread")], monkeypatch
    )
    asyncio.run(run_classification(coordinator))

    assert len(coordinator.hass.scheduled) == 1
    assert coordinator.store.saved == []


def test_explicit_correction_is_persisted_immediately(monkeypatch) -> None:
    coordinator = make_coordinator([item("uid-1", "Milk")], monkeypatch)

    asyncio.run(coordinator.async_set_section("uid-1", "Produce", remember=True))

    assert len(coordinator.store.saved) == 1
    assert coordinator.store.saved[0]["entries"]["milk"]["section"] == "Produce"


def test_shutdown_flushes_cache_and_cancels_tasks(monkeypatch) -> None:
    coordinator = make_coordinator([item("uid-1", "Milk")], monkeypatch)

    asyncio.run(coordinator.async_shutdown())

    assert coordinator.store.saved
    assert coordinator._tasks == set()


def test_section_names_tolerate_extra_persisted_keys(monkeypatch) -> None:
    monkeypatch.setattr(storage_module, "CacheStore", FakeStore)
    monkeypatch.setattr(coordinator_module, "PriceCoordinator", FakePriceCoordinator)
    entry = SimpleNamespace(
        entry_id="synthetic-entry",
        title="Example Supermarket",
        data={
            "profile_name": "Example Supermarket",
            "sections": [{"name": "Produce", "hints": "fruit", "future_key": 1}],
            "source_entity_id": "todo.synthetic_source",
            "ai_task_entity_id": "ai_task.synthetic",
        },
    )
    hass = SimpleNamespace(data={}, states=SimpleNamespace(get=lambda _e: None))

    coordinator = RouteCoordinator(hass, entry)

    assert coordinator.sections == (RouteSection("Produce", "fruit"),)


def test_pending_save_is_cancelled_on_shutdown(monkeypatch) -> None:
    """A debounced write must not survive shutdown and rewrite the cache."""

    coordinator = make_coordinator([item("uid-1", "Milk")], monkeypatch)
    cancelled: list[bool] = []

    class Handle:
        def cancel(self) -> None:
            cancelled.append(True)

    coordinator._save_handle = Handle()

    asyncio.run(coordinator.async_shutdown())

    assert cancelled == [True]
    assert coordinator._save_handle is None


def test_flush_after_shutdown_does_not_write(monkeypatch) -> None:
    coordinator = make_coordinator([item("uid-1", "Milk")], monkeypatch)
    asyncio.run(coordinator.async_shutdown())
    saved_before = len(coordinator.store.saved)

    coordinator._async_flush_save()

    assert len(coordinator.store.saved) == saved_before
    assert coordinator._save_tasks == set()


def test_shutdown_waits_for_an_in_flight_debounced_save(monkeypatch) -> None:
    """Removal deletes the file only after any pending write has landed."""

    coordinator = make_coordinator([item("uid-1", "Milk")], monkeypatch)
    order: list[str] = []
    coordinator.cache.set("Milk", "Dairy")
    original = coordinator.store.async_save

    async def slow_save(cache):
        await asyncio.sleep(0)
        order.append(f"save:{len(cache.entries)}")
        await original(cache)

    coordinator.store.async_save = slow_save

    async def scenario() -> None:
        coordinator._schedule_save()
        coordinator._async_flush_save()
        await coordinator.async_shutdown()

    asyncio.run(scenario())

    # The debounced write is awaited first, then the final shutdown save.
    assert order == ["save:1", "save:1"]


def test_final_write_flushes_a_pending_debounced_save(monkeypatch) -> None:
    """Home Assistant stops without unloading entries, so flush on final write."""

    coordinator = make_coordinator([item("uid-1", "Milk")], monkeypatch)
    coordinator.cache.set("Milk", "Dairy")
    asyncio.run(coordinator.async_setup())
    coordinator._schedule_save()
    assert coordinator.store.saved == []

    async def stop() -> None:
        for _event, callback in list(coordinator.hass.final_write_listeners):
            await callback(SimpleNamespace())

    asyncio.run(stop())

    assert coordinator.store.saved
    assert coordinator.store.saved[-1]["entries"]["milk"]["section"] == "Dairy"
    assert coordinator._save_handle is None


def test_final_write_listener_is_registered_and_removed(monkeypatch) -> None:
    coordinator = make_coordinator([item("uid-1", "Milk")], monkeypatch)

    asyncio.run(coordinator.async_setup())
    assert len(coordinator.hass.final_write_listeners) == 1

    asyncio.run(coordinator.async_shutdown())
    assert coordinator.hass.final_write_listeners == []


def test_save_is_immediate_while_home_assistant_is_stopping(monkeypatch) -> None:
    """A debounce that outlives shutdown would silently drop the write."""

    coordinator = make_coordinator([item("uid-1", "Milk")], monkeypatch)
    coordinator.hass.state = CoreState.stopping
    scheduled_now: list[object] = []
    coordinator.hass.loop = SimpleNamespace(
        call_later=lambda *_args: pytest.fail("should not debounce while stopping"),
        call_soon=lambda callback: scheduled_now.append(callback)
        or SimpleNamespace(cancel=lambda: None),
    )

    coordinator._schedule_save()

    assert len(scheduled_now) == 1


def test_closed_coordinator_refuses_mutations(monkeypatch) -> None:
    """A Refresh button left alive after a failed unload must not write."""

    from homeassistant.exceptions import HomeAssistantError

    coordinator = make_coordinator([item("uid-1", "Milk")], monkeypatch)
    asyncio.run(coordinator.async_shutdown())
    saved_before = len(coordinator.store.saved)

    asyncio.run(coordinator.async_refresh())
    coordinator.async_classify_unknown()
    assert len(coordinator.store.saved) == saved_before

    for mutation in (
        coordinator.async_set_section("uid-1", "Produce", True),
        coordinator.async_forget("uid-1"),
        coordinator.async_reclassify_all(),
        coordinator.async_clear_cache(True),
    ):
        with pytest.raises(HomeAssistantError):
            asyncio.run(mutation)
