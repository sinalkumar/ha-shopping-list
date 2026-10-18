"""Service targeting and config entry lifecycle."""

import asyncio
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ServiceValidationError

import smart_shopping_route as integration
import smart_shopping_route.storage as storage_module
from smart_shopping_route.const import (
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    SERVICE_CLEAR_CACHE,
    SERVICE_REFRESH,
)

ROUTE_ENTITY = "todo.example_supermarket"
OTHER_ROUTE_ENTITY = "todo.second_shop"
FOREIGN_ENTITY = "todo.some_other_integration"


class _FakePriceStore:
    """Removal must also delete the price store; nothing else is exercised here."""

    removed: ClassVar[list[str]] = []

    def __init__(self, _hass, entry_id) -> None:
        self.entry_id = entry_id

    async def async_remove(self) -> None:
        _FakePriceStore.removed.append(self.entry_id)


def registry_entry(platform: str, config_entry_id: str = "synthetic-entry"):
    return SimpleNamespace(
        domain="todo", platform=platform, config_entry_id=config_entry_id
    )


def call(**data):
    return SimpleNamespace(data=data)


@pytest.fixture
def hass(monkeypatch):
    """A hass stand-in whose target expansion is scriptable."""

    state = SimpleNamespace(
        expanded={"referenced": set(), "indirectly_referenced": set()},
        registry={
            ROUTE_ENTITY: registry_entry(DOMAIN),
            OTHER_ROUTE_ENTITY: registry_entry(DOMAIN, "second-entry"),
            FOREIGN_ENTITY: registry_entry("todoist"),
        },
        services={},
        data={},
    )
    fake = SimpleNamespace(
        data={},
        services=SimpleNamespace(
            has_service=lambda domain, service: (domain, service) in state.services,
            async_register=lambda domain,
            service,
            handler,
            schema: state.services.__setitem__((domain, service), handler),
            async_remove=lambda domain, service: state.services.pop(
                (domain, service), None
            ),
        ),
        config_entries=SimpleNamespace(),
        state=state,
    )
    monkeypatch.setattr(
        integration,
        "async_extract_referenced_entity_ids",
        lambda _hass, _selection: SimpleNamespace(
            referenced=state.expanded["referenced"],
            indirectly_referenced=state.expanded["indirectly_referenced"],
        ),
    )
    monkeypatch.setattr(
        integration.er,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=state.registry.get),
    )
    return fake


def test_entity_target_resolves_to_the_route(hass) -> None:
    hass.state.expanded["referenced"] = {ROUTE_ENTITY}
    hass.data[DOMAIN] = {"synthetic-entry": "coordinator"}

    assert integration._coordinator_for_call(hass, call()) == "coordinator"


def test_indirect_targets_are_resolved_too(hass) -> None:
    """A device, area, floor, or label target must work like an entity target."""

    hass.state.expanded["indirectly_referenced"] = {ROUTE_ENTITY}
    hass.data[DOMAIN] = {"synthetic-entry": "coordinator"}

    assert integration._coordinator_for_call(hass, call()) == "coordinator"


def test_foreign_todo_entities_do_not_make_a_target_ambiguous(hass) -> None:
    hass.state.expanded["referenced"] = {ROUTE_ENTITY}
    hass.state.expanded["indirectly_referenced"] = {FOREIGN_ENTITY}
    hass.data[DOMAIN] = {"synthetic-entry": "coordinator"}

    assert integration._coordinator_for_call(hass, call()) == "coordinator"


def test_ambiguity_across_direct_and_indirect_route_entities_is_rejected(hass) -> None:
    """Dropping either half of the union would let this pass silently."""

    hass.state.expanded["referenced"] = {ROUTE_ENTITY}
    hass.state.expanded["indirectly_referenced"] = {OTHER_ROUTE_ENTITY}
    hass.data[DOMAIN] = {
        "synthetic-entry": "coordinator",
        "second-entry": "second-coordinator",
    }

    with pytest.raises(ServiceValidationError, match="exactly one"):
        integration._coordinator_for_call(hass, call())


def test_target_selection_is_built_from_the_service_call(hass, monkeypatch) -> None:
    """The real TargetSelection must receive the call data, not an empty dict."""

    seen: list[object] = []
    monkeypatch.setattr(
        integration, "TargetSelection", lambda data: seen.append(data) or object()
    )
    hass.state.expanded["referenced"] = {ROUTE_ENTITY}
    hass.data[DOMAIN] = {"synthetic-entry": "coordinator"}

    integration._coordinator_for_call(hass, call(entity_id=[ROUTE_ENTITY]))

    assert seen == [{"entity_id": [ROUTE_ENTITY]}]


def test_real_target_selection_accepts_every_target_form(hass, monkeypatch) -> None:
    """The real TargetSelection (pure, no hass) must see all target keys."""

    from homeassistant.helpers.target import TargetSelection

    monkeypatch.setattr(integration, "TargetSelection", TargetSelection)
    seen: list[object] = []
    monkeypatch.setattr(
        integration,
        "async_extract_referenced_entity_ids",
        lambda _hass, selection: seen.append(selection)
        or SimpleNamespace(referenced={ROUTE_ENTITY}, indirectly_referenced=set()),
    )
    hass.data[DOMAIN] = {"synthetic-entry": "coordinator"}

    for data in (
        {"entity_id": [ROUTE_ENTITY]},
        {"device_id": "device-1"},
        {"area_id": "kitchen"},
        {"floor_id": "ground"},
        {"label_id": "shopping"},
    ):
        seen.clear()
        integration._coordinator_for_call(hass, call(**data))
        assert seen[0].has_any_target, data


def test_foreign_todo_entities_are_ignored(hass) -> None:
    hass.state.expanded["referenced"] = {FOREIGN_ENTITY}
    hass.data[DOMAIN] = {"synthetic-entry": "coordinator"}

    with pytest.raises(ServiceValidationError, match="exactly one"):
        integration._coordinator_for_call(hass, call())


def test_ambiguous_target_is_rejected(hass) -> None:
    hass.state.expanded["referenced"] = {ROUTE_ENTITY, OTHER_ROUTE_ENTITY}
    hass.data[DOMAIN] = {
        "synthetic-entry": "coordinator",
        "second-entry": "second-coordinator",
    }

    with pytest.raises(ServiceValidationError, match="exactly one"):
        integration._coordinator_for_call(hass, call())


def test_unloaded_profile_is_reported(hass) -> None:
    hass.state.expanded["referenced"] = {ROUTE_ENTITY}
    hass.data[DOMAIN] = {}

    with pytest.raises(ServiceValidationError, match="not found"):
        integration._coordinator_for_call(hass, call())


def test_item_uid_is_required() -> None:
    with pytest.raises(ServiceValidationError, match="UID"):
        integration._item_uid(call())
    assert integration._item_uid(call(item_uid="uid-1")) == "uid-1"


def test_services_are_registered_once_for_all_profiles(hass) -> None:
    integration._async_register_services(hass)
    integration._async_register_services(hass)

    assert (DOMAIN, SERVICE_REFRESH) in hass.state.services
    assert (DOMAIN, SERVICE_CLEAR_CACHE) in hass.state.services


@pytest.mark.parametrize(
    "target",
    [
        {"entity_id": [ROUTE_ENTITY]},
        {"device_id": "device-1"},
        {"area_id": "kitchen"},
        {"floor_id": "ground"},
        {"label_id": "shopping"},
    ],
)
@pytest.mark.parametrize("denied", [ROUTE_ENTITY, "todo.source", None])
def test_mutation_auth_checks_resolved_route_and_source(hass, target, denied):
    coordinator = SimpleNamespace(
        source=SimpleNamespace(entity_id="todo.source"), async_refresh=AsyncMock()
    )
    hass.state.expanded["indirectly_referenced"] = {ROUTE_ENTITY}
    hass.data[DOMAIN] = {"synthetic-entry": coordinator}
    hass.auth = SimpleNamespace(
        async_get_user=AsyncMock(
            return_value=SimpleNamespace(
                permissions=SimpleNamespace(
                    check_entity=lambda entity, _policy: entity != denied
                )
            )
        )
    )
    integration._async_register_services(hass)
    request = call(**target)
    request.context = SimpleNamespace(user_id="synthetic-user")
    if denied:
        with pytest.raises(ServiceValidationError, match="Not authorized"):
            asyncio.run(hass.state.services[DOMAIN, SERVICE_REFRESH](request))
        coordinator.async_refresh.assert_not_awaited()
    else:
        asyncio.run(hass.state.services[DOMAIN, SERVICE_REFRESH](request))
        coordinator.async_refresh.assert_awaited_once()


def test_removed_entry_tears_down_a_surviving_coordinator(hass, monkeypatch) -> None:
    """A failed unload must not leave a pending save that recreates the cache."""

    events: list[str] = []

    class Coordinator:
        async def async_shutdown(self) -> None:
            events.append("shutdown")

    class Store:
        def __init__(self, _hass, entry_id) -> None:
            self.entry_id = entry_id

        async def async_remove(self) -> None:
            events.append(f"remove:{self.entry_id}")

    monkeypatch.setattr(
        integration, "async_pop_store", lambda _hass, entry_id: Store(_hass, entry_id)
    )
    monkeypatch.setattr(integration, "PriceStore", _FakePriceStore)
    hass.data[DOMAIN] = {"synthetic-entry": Coordinator()}
    entry = SimpleNamespace(entry_id="synthetic-entry")

    asyncio.run(integration.async_remove_entry(hass, entry))

    assert events == ["shutdown", "remove:synthetic-entry"]
    assert DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]


def test_removed_entry_deletes_the_cache_without_a_coordinator(
    hass, monkeypatch
) -> None:
    events: list[str] = []

    class Store:
        def __init__(self, _hass, entry_id) -> None:
            self.entry_id = entry_id

        async def async_remove(self) -> None:
            events.append(f"remove:{self.entry_id}")

    monkeypatch.setattr(
        integration, "async_pop_store", lambda _hass, entry_id: Store(_hass, entry_id)
    )
    monkeypatch.setattr(integration, "PriceStore", _FakePriceStore)

    asyncio.run(
        integration.async_remove_entry(hass, SimpleNamespace(entry_id="gone-entry"))
    )

    assert events == ["remove:gone-entry"]


def test_migration_accepts_the_current_version(hass) -> None:
    entry = SimpleNamespace(version=CONFIG_ENTRY_VERSION)
    assert asyncio.run(integration.async_migrate_entry(hass, entry)) is True


def test_migration_refuses_a_newer_version(hass) -> None:
    entry = SimpleNamespace(version=CONFIG_ENTRY_VERSION + 1)
    assert asyncio.run(integration.async_migrate_entry(hass, entry)) is False


def test_last_unload_removes_the_services(hass, monkeypatch) -> None:
    integration._async_register_services(hass)
    coordinator = SimpleNamespace(async_shutdown=lambda: asyncio.sleep(0))
    hass.data[DOMAIN] = {"synthetic-entry": coordinator}

    async def unload_platforms(_entry, _platforms):
        return True

    hass.config_entries = SimpleNamespace(async_unload_platforms=unload_platforms)
    entry = SimpleNamespace(entry_id="synthetic-entry")

    assert asyncio.run(integration.async_unload_entry(hass, entry)) is True
    assert hass.state.services == {}


def test_failed_unload_keeps_the_services(hass) -> None:
    integration._async_register_services(hass)
    hass.data[DOMAIN] = {"synthetic-entry": SimpleNamespace()}

    async def unload_platforms(_entry, _platforms):
        return False

    hass.config_entries = SimpleNamespace(async_unload_platforms=unload_platforms)
    entry = SimpleNamespace(entry_id="synthetic-entry")

    assert asyncio.run(integration.async_unload_entry(hass, entry)) is False
    assert (DOMAIN, SERVICE_REFRESH) in hass.state.services


class FakeCoordinator:
    """Coordinator stand-in for setup/unload sequencing."""

    def __init__(self, _hass, _entry) -> None:
        self.shutdown = 0
        self.setup_error: Exception | None = None

    async def async_setup(self) -> None:
        if self.setup_error is not None:
            raise self.setup_error

    async def async_shutdown(self) -> None:
        self.shutdown += 1


def test_setup_retries_only_for_home_assistant_errors(hass, monkeypatch) -> None:
    from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError

    monkeypatch.setattr(integration, "RouteCoordinator", FakeCoordinator)
    entry = SimpleNamespace(entry_id="synthetic-entry")

    coordinator_holder: list[FakeCoordinator] = []

    def factory(_hass, _entry):
        coordinator = FakeCoordinator(_hass, _entry)
        coordinator.setup_error = HomeAssistantError("source missing")
        coordinator_holder.append(coordinator)
        return coordinator

    monkeypatch.setattr(integration, "RouteCoordinator", factory)

    with pytest.raises(ConfigEntryNotReady):
        asyncio.run(integration.async_setup_entry(hass, entry))


def test_setup_does_not_swallow_unexpected_bugs(hass, monkeypatch) -> None:
    def factory(_hass, _entry):
        coordinator = FakeCoordinator(_hass, _entry)
        coordinator.setup_error = RuntimeError("bug")
        return coordinator

    monkeypatch.setattr(integration, "RouteCoordinator", factory)

    with pytest.raises(RuntimeError, match="bug"):
        asyncio.run(integration.async_setup_entry(hass, SimpleNamespace(entry_id="e")))


def test_setup_cleans_up_when_platform_forwarding_fails(hass, monkeypatch) -> None:
    coordinators: list[FakeCoordinator] = []

    def factory(_hass, _entry):
        coordinator = FakeCoordinator(_hass, _entry)
        coordinators.append(coordinator)
        return coordinator

    async def forward(_entry, _platforms):
        raise RuntimeError("platform failed")

    monkeypatch.setattr(integration, "RouteCoordinator", factory)
    hass.config_entries = SimpleNamespace(async_forward_entry_setups=forward)
    entry = SimpleNamespace(entry_id="synthetic-entry")

    with pytest.raises(RuntimeError, match="platform failed"):
        asyncio.run(integration.async_setup_entry(hass, entry))

    assert coordinators[0].shutdown == 1
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


def test_setup_registers_the_coordinator(hass, monkeypatch) -> None:
    async def forward(_entry, _platforms):
        return True

    monkeypatch.setattr(integration, "RouteCoordinator", FakeCoordinator)
    hass.config_entries = SimpleNamespace(async_forward_entry_setups=forward)
    entry = SimpleNamespace(entry_id="synthetic-entry")

    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True
    assert isinstance(hass.data[DOMAIN]["synthetic-entry"], FakeCoordinator)


def test_removal_reuses_the_profiles_own_store_instance(hass, monkeypatch) -> None:
    """Store cancels its final-write listener per instance.

    Removal through a fresh instance would leave the coordinator's pending
    write armed, and that write would recreate the cache file during shutdown.
    """

    from smart_shopping_route.storage import STORES_DATA_KEY, async_get_store

    events: list[str] = []

    class Store:
        def __init__(self, _hass, entry_id) -> None:
            self.entry_id = entry_id

        async def async_remove(self) -> None:
            events.append(f"remove:{self.entry_id}:{id(self)}")

    monkeypatch.setattr(storage_module, "CacheStore", Store)
    monkeypatch.setattr(integration, "PriceStore", _FakePriceStore)

    class Coordinator:
        def __init__(self) -> None:
            self.shutdown = 0

        async def async_shutdown(self) -> None:
            self.shutdown += 1

    coordinator = Coordinator()
    hass.data[DOMAIN] = {"synthetic-entry": coordinator}
    # Register through the real helper so this test fails if async_pop_store
    # ever starts returning a fresh instance instead of the registered one.
    coordinator_store = async_get_store(hass, "synthetic-entry")

    asyncio.run(
        integration.async_remove_entry(
            hass, SimpleNamespace(entry_id="synthetic-entry")
        )
    )

    assert coordinator.shutdown == 1
    assert events == [f"remove:synthetic-entry:{id(coordinator_store)}"]
    assert hass.data[STORES_DATA_KEY] == {}


def test_pin_service_requires_pricing(hass, monkeypatch) -> None:
    hass.state.expanded["referenced"] = {ROUTE_ENTITY}
    coordinator = SimpleNamespace(
        prices=SimpleNamespace(enabled=False), item_by_uid=lambda _uid: None
    )
    hass.data[DOMAIN] = {"synthetic-entry": coordinator}
    integration._async_register_services(hass)
    handler = hass.state.services[DOMAIN, "pin_item_product"]

    with pytest.raises(ServiceValidationError, match="Pricing is not enabled"):
        asyncio.run(handler(call(item_uid="uid-1")))


def test_pin_service_pins_the_item(hass) -> None:
    pinned: list[dict] = []

    class Prices:
        enabled = True

        async def async_pin(
            self, summary, *, query="", product_id=None, grocer_product_id=None
        ):
            pinned.append({
                "summary": summary,
                "query": query,
                "product_id": product_id,
            })

    coordinator = SimpleNamespace(
        prices=Prices(),
        item_by_uid=lambda _uid: SimpleNamespace(summary="Kids vitamins"),
        async_notify_prices=lambda: None,
    )
    hass.state.expanded["referenced"] = {ROUTE_ENTITY}
    hass.data[DOMAIN] = {"synthetic-entry": coordinator}
    integration._async_register_services(hass)
    handler = hass.state.services[DOMAIN, "pin_item_product"]

    asyncio.run(
        handler(
            call(
                item_uid="uid-1",
                query="Kids Multivitamin Gummies",
                product_id="1234567",
            )
        )
    )

    assert pinned == [
        {
            "summary": "Kids vitamins",
            "query": "Kids Multivitamin Gummies",
            "product_id": "1234567",
        }
    ]


def test_unpin_service_forgets_the_item(hass) -> None:
    unpinned: list[str] = []

    class Prices:
        enabled = True

        async def async_unpin(self, summary):
            unpinned.append(summary)

    coordinator = SimpleNamespace(
        prices=Prices(),
        item_by_uid=lambda _uid: SimpleNamespace(summary="Kids vitamins"),
        async_notify_prices=lambda: None,
    )
    hass.state.expanded["referenced"] = {ROUTE_ENTITY}
    hass.data[DOMAIN] = {"synthetic-entry": coordinator}
    integration._async_register_services(hass)
    handler = hass.state.services[DOMAIN, "unpin_item_product"]

    asyncio.run(handler(call(item_uid="uid-1")))

    assert unpinned == ["Kids vitamins"]


def test_removal_also_deletes_the_price_store(hass, monkeypatch) -> None:
    """Pins and price history are user data and must not outlive the profile."""

    _FakePriceStore.removed.clear()
    monkeypatch.setattr(integration, "PriceStore", _FakePriceStore)
    monkeypatch.setattr(
        integration, "async_pop_store", lambda _hass, entry_id: _NullStore()
    )

    asyncio.run(
        integration.async_remove_entry(hass, SimpleNamespace(entry_id="gone-entry"))
    )

    assert _FakePriceStore.removed == ["gone-entry"]


class _NullStore:
    async def async_remove(self) -> None:
        return None


def test_pin_service_reports_an_unknown_uid(hass) -> None:
    def item_by_uid(_uid):
        raise ValueError("Unknown source item UID")

    coordinator = SimpleNamespace(
        prices=SimpleNamespace(enabled=True),
        item_by_uid=item_by_uid,
        async_notify_prices=lambda: None,
    )
    hass.state.expanded["referenced"] = {ROUTE_ENTITY}
    hass.data[DOMAIN] = {"synthetic-entry": coordinator}
    integration._async_register_services(hass)

    with pytest.raises(ServiceValidationError, match="Unknown source item UID"):
        asyncio.run(
            hass.state.services[DOMAIN, "pin_item_product"](call(item_uid="nope"))
        )


def test_unpin_service_reports_an_unknown_uid(hass) -> None:
    def item_by_uid(_uid):
        raise ValueError("Unknown source item UID")

    coordinator = SimpleNamespace(
        prices=SimpleNamespace(enabled=True), item_by_uid=item_by_uid
    )
    hass.state.expanded["referenced"] = {ROUTE_ENTITY}
    hass.data[DOMAIN] = {"synthetic-entry": coordinator}
    integration._async_register_services(hass)

    with pytest.raises(ServiceValidationError, match="Unknown source item UID"):
        asyncio.run(
            hass.state.services[DOMAIN, "unpin_item_product"](call(item_uid="nope"))
        )


def test_pin_service_leaves_notification_to_the_price_coordinator(hass) -> None:
    """The price coordinator's update callback notifies; the service must not.

    Dispatching here as well writes every price-aware entity's state twice per
    pin, and the coordinator covers the case where its refresh is deferred.
    """

    notifications: list[bool] = []

    class Prices:
        enabled = True

        async def async_pin(
            self, summary, *, query="", product_id=None, grocer_product_id=None
        ):
            return None

        async def async_unpin(self, summary):
            return None

    coordinator = SimpleNamespace(
        prices=Prices(),
        item_by_uid=lambda _uid: SimpleNamespace(summary="Kids vitamins"),
        async_notify_prices=lambda: notifications.append(True),
    )
    hass.state.expanded["referenced"] = {ROUTE_ENTITY}
    hass.data[DOMAIN] = {"synthetic-entry": coordinator}
    integration._async_register_services(hass)

    asyncio.run(hass.state.services[DOMAIN, "pin_item_product"](call(item_uid="uid-1")))
    asyncio.run(
        hass.state.services[DOMAIN, "unpin_item_product"](call(item_uid="uid-1"))
    )

    assert notifications == []
