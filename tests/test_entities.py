"""Status sensor and refresh button entities."""

import asyncio
from types import SimpleNamespace

from smart_shopping_route.button import RefreshRouteButton
from smart_shopping_route.device import route_device_info
from smart_shopping_route.sensor import UnclassifiedSensor


class Coordinator:
    available = True
    ai_available = True
    signal = "smart_shopping_route_synthetic-entry_update"

    def __init__(self) -> None:
        self.source = SimpleNamespace(entity_id="todo.synthetic_source")
        self.unclassified_count = 3
        self.retry_scheduled_count = 1
        self.pending = {"milk", "bread"}
        self.cache = SimpleNamespace(entries={"milk": object(), "bread": object()})
        self.refreshed = 0

    async def async_refresh(self) -> None:
        self.refreshed += 1


def entry():
    return SimpleNamespace(
        entry_id="synthetic-entry",
        title="Example Supermarket",
        data={"profile_name": "Example Supermarket"},
    )


def test_sensor_reports_aggregates_only() -> None:
    sensor = UnclassifiedSensor(entry(), Coordinator())

    assert sensor.native_value == 3
    assert sensor.extra_state_attributes == {
        "cached_classifications": 2,
        "pending_classifications": 2,
        "scheduled_retries": 1,
        "ai_available": True,
    }


def test_sensor_availability_follows_the_coordinator() -> None:
    coordinator = Coordinator()
    sensor = UnclassifiedSensor(entry(), coordinator)

    assert sensor.available is True
    coordinator.available = False
    assert sensor.available is False


def test_sensor_is_grouped_under_the_profile_device() -> None:
    sensor = UnclassifiedSensor(entry(), Coordinator())

    assert sensor.device_info["identifiers"] == {
        ("smart_shopping_route", "synthetic-entry")
    }
    assert sensor.unique_id == "synthetic-entry_unclassified"


def test_button_refreshes_the_route() -> None:
    coordinator = Coordinator()
    button = RefreshRouteButton(entry(), coordinator)

    asyncio.run(button.async_press())

    assert coordinator.refreshed == 1


def test_button_is_grouped_under_the_profile_device() -> None:
    button = RefreshRouteButton(entry(), Coordinator())

    assert button.device_info["identifiers"] == {
        ("smart_shopping_route", "synthetic-entry")
    }
    assert button.unique_id == "synthetic-entry_refresh"


def test_device_info_falls_back_to_the_entry_title() -> None:
    without_profile_name = SimpleNamespace(
        entry_id="synthetic-entry", title="Fallback name", data={}
    )

    info = route_device_info(without_profile_name)

    assert info["name"] == "Fallback name"


def test_button_refreshes_availability_when_the_source_changes() -> None:
    """A derived availability must be re-written, not computed once at setup.

    On a live install the refresh button sat at "unavailable" forever while the
    route entity was healthy, because only subscribing entities re-write state.
    """

    import smart_shopping_route.button as button_module

    coordinator = Coordinator()
    button = RefreshRouteButton(entry(), coordinator)

    connections: list[tuple[str, object]] = []
    tracked: list[list[str]] = []

    class FakeDispatcher:
        @staticmethod
        def connect(_hass, signal, target):
            connections.append((signal, target))
            return lambda: None

    class FakeTrack:
        @staticmethod
        def __call__(_hass, entity_ids, _target):
            tracked.append(list(entity_ids))
            return lambda: None

    class FakeEntity:
        hass = object()

        def async_on_remove(self, _cb):
            return None

        async def async_added_to_hass(self):
            return None

    button.async_on_remove = FakeEntity().async_on_remove
    original_connect = button_module.async_dispatcher_connect
    original_track = button_module.async_track_state_change_event
    button_module.async_dispatcher_connect = FakeDispatcher.connect
    button_module.async_track_state_change_event = FakeTrack()
    try:
        asyncio.run(button.async_added_to_hass())
    finally:
        button_module.async_dispatcher_connect = original_connect
        button_module.async_track_state_change_event = original_track

    assert connections, "button must subscribe to coordinator updates"
    assert connections[0][0] == coordinator.signal
    assert tracked and tracked[0] == ["todo.synthetic_source"]


def test_button_state_write_does_not_raise() -> None:
    coordinator = Coordinator()
    button = RefreshRouteButton(entry(), coordinator)
    written: list[bool] = []
    button.async_write_ha_state = lambda: written.append(True)

    button._handle_update()
    button._handle_dependency_state(None)

    assert written == [True, True]
